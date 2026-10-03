# 🚀 ALETH STUDIO: MASTER AGENT HANDOFF & ARCHITECTURE SPECIFICATION

> **CRITICAL DIRECTIVE FOR ANY INCOMING AGENT:**
> Read this document completely before modifying or executing any code. This single
> document contains the full architecture, execution flow, component map, strict
> constraints, and current state. You will not need to ask redundant questions or
> hallucinate context.
>
> Everything in §2, §5, §6 and §9 was verified against the working tree at the time of
> the last edit. Where a claim can drift (file inventories, current plan state), it is
> stated as of that edit rather than as a permanent fact. §9 in particular reflects
> `PLAN.md` as of the last rewrite — **re-read `PLAN.md` itself before trusting it**.

---

## ⚡ 0. Current State at This Handoff (Phase 42)

- **Phases 5 → 42 are complete and CI-green.** Phase 42 (daemon lifecycle and concurrency
  hardening) is the latest. `MASTER_CONTEXT.md` is the ground truth; this section only points at
  what changed most recently.
- **A signal is a request, not a kill (Phase 42).** `tools/lifecycle.py` holds one process-wide
  `threading.Event`. `install_shutdown_sweep`'s handler flips it, **drains** the components that
  registered their own stop (`ApiServer.stop`), sweeps the containers, and exits `0` -- so the
  process lock deletes `aleth.pid` on the way out rather than leaving it for the next boot to reap.
  The HTTP `_guard` answers **503** to new requests (after the loopback gate), and the agent loop
  stops at the top of its next step and commits its memory blob before raising. `cli._boot`
  (`aleth boot`) now arms the same teardown, which it previously never did -- only `main.py`
  installed it, and `aleth boot` reaches `app` directly.
- **The database half is the one connection factory (Phase 28, re-asserted Phase 42).**
  `storage/connection.py` sets `journal_mode=WAL`, `synchronous=NORMAL` and `busy_timeout=5000` on
  every connection, so a status poll cannot collide with a task write; `tests/test_state_store.py`
  now asserts all three rather than WAL alone.
- **Secrets are resolved, not exported (Phase 41).** `tools/secrets.py` has a `SecretStore` bound to
  a `contextvars.ContextVar`; `credential(env_name)` is the single read path and the client factories
  (`core/config.py`, `orchestration/system2.py`, `tools/system2_cost.py`) call it. There is **no**
  `os.environ` mutation anywhere: the environment is read as a last resort and never written.
- **`keyring` is a hard dependency (Phase 41).** A module-level import, no lazy fallback: optional
  security is no security, because a checkout that boots without it is one whose users fall back to
  plaintext. CI installs it from `pyproject.toml`; a dev venv created before it was declared must be
  reinstalled (`pip install -e ".[test]"`).
- **The process lock is deterministic (Phase 41).** `tools/process_lock.py` holds
  `<state_dir>/aleth.pid` under an exclusive OS file lock for the daemon's lifetime; a live holder
  refuses the boot with the pid named, and a stale lock from a hard crash is reaped -- running the
  Phase 36 sweep at the moment the crash is discovered. `check_process_lock` is the authoritative
  pre-flight check; `check_port` stays beside it for the one case the lock cannot see (some *other*
  program holding the port).
- **Credentials live in the OS keyring (Phase 40).** `aleth keys set/clear/list`; `list` reports
  names only.
- **The record is structured and bounded (Phase 40).** `tools/engine_log.py`: one JSON object per
  line with the run's correlation id, on a 50 MB × 3 rotating handler under the state directory.
- **The engine refuses to start half-broken (Phase 40).** `aleth boot` pre-flights the container
  runtime, `git`, the API port and the credential store, and aborts with a fix line per failure.
- **Truncation is strict (Phase 39).** A rewind deletes the abandoned timeline on **both** sides:
  `IntentLedger.truncate_to_step` deletes the rows and `snapshots.truncate_after` drops the tags, so
  the resumed run continues at `target + 1` instead of colliding with its own ghost. Linear history
  is the contract; if a person wants step 5, they should not rewind to step 2.
- **The egress is crash-recoverable (Phase 39).** `apply_egress` writes an uncapped,
  `git apply`-able `aleth_egress.patch` into the host root *before* the first byte changes, and
  removes it only once the whole egress has landed.
- **The memory is durable (Phase 37).** `intent_step_spend.context_blob` carries the loop's exact
  memory at every step, and a rewind hydrates from it -- so a rewind is exact across a pass
  boundary, and `State(t) = Files(t) + Memory(t)` holds. `run_tool_loop(resume_context=…)` is the
  injection seam; the boot drain is the automatic path. Truncating the bill now *zeroes* the later
  steps' spend rather than deleting their rows, so a second rewind still finds their memory.
- **The work can leave the sandbox (Phase 38).** `create_staging` records the host baseline;
  `POST /api/intent/egress` applies a verified shadow to the host atomically and refuses on a
  collision with a human edit. `workspace_merge(approve=True)` delegates to it, so there is one
  apply path. The UI's **Apply to Project** appears in the top bar when the engine reports
  `mergeable`.
- **A run can be rewound (Phase 35).** Every shadow carries its own git repository
  (`tools/snapshots.py`); `create_staging` commits the workspace as `step 0`, and each
  file-changing tool call is committed and tagged. `POST /api/intent/rollback` restores the tree
  (only while paused -- the atomic ledger write is the gate), and the loop restores the context
  window from a per-step snapshot while `IntentLedger.truncate_to_step` truncates the bill.
  `GET /api/intent/steps` lists the rewind points; the steering overlay has a step picker.
- **The engine cleans up after itself (Phase 36).** `install_shutdown_sweep(stage, extra=…)` runs
  `abort_running_intents` on `atexit`/SIGINT/SIGTERM: every `running` intent becomes
  `aborted_by_system` (its own terminal status, and it gates new work) before the process leaves.
  The boot reaper is the container sweep; `sweep_orphaned_networks` runs at every boot because the
  engine creates no networks, and the claim is checked rather than promised.
- **Shadows are removable again.** Git writes loose objects mode 0444, which made a plain `rmtree`
  fail on Windows; all removals go through `staging.remove_tree`.
- **The stream is rendered, not dropped (Phase 34).** `ui/js/console.js` echoes the loop's
  `agent_thought` / `tool_execution_start` / `tool_execution_complete` / `intent_paused|steered|resumed`
  into the transcript, and `token_budget_update` drives a live burn counter in the console bar
  (spent vs the event's own `limit`; amber past 80%, red at the ceiling).
- **The run is steerable from the screen (Phase 34).** An `Interrupt` control beside Stop posts to
  `/api/intent/interrupt`; an `intent_paused` event opens a steering overlay whose two buttons
  release the hold through `/api/intent/resume` (with the correction, or without one). A refused
  resume keeps the overlay up and says why.
- **One re-targetable connection (Phase 34).** `ui/js/stream.js` owns the single SSE connection and
  its path; the window follows `/api/intents/<id>/stream` while a run is live and returns to
  `/api/events` when it ends. The server filter passes *this intent's* frames and untagged lifecycle
  frames, so the terminal event is never dropped.
- **Reconnect hydrates from the ledger (Phase 34).** `live-run.hydrateRunState` shows the hold for a
  paused run, finalizes a run that ended during the outage, and adopts the live run on a reload --
  from the durable ledger alone, never `get_run_state` (the thread lags a fresh launch). No replay,
  so no duplicated lines.
- **The run streams itself (Phase 33).** The agent loop emits `agent_thought`,
  `tool_execution_start`/`_complete` and `token_budget_update` through an injected `emit` threaded
  from `EngineService.emit_event` down to `run_tool_loop`; each is a member of the strict bus union
  (`tools/payloads.py`) and mirrored in `ui/js/bridge-bus.js`. `GET /api/intents/{id}/stream` is a
  gateway *route* that filters the hub's frames to one run; `/api/events` stays the unfiltered
  firehose, byte-for-byte.
- **A run is steerable (Phase 33).** `POST /api/intent/interrupt` holds a running intent
  (`paused_awaiting_input`, not `failed`) and `POST /api/intent/resume` releases it with the
  correction queued in the ledger. The loop's gate announces `intent_paused`, waits (≤ 900 s), drains
  the correction exactly once and injects it as a `[user]` message; the gate drains on the *running*
  path too, so a resume that beats the next gate cannot strand the correction.
- **The summary cannot grow (Phase 33).** `_compact` is recursive and its output is hard-capped at
  `MAX_SUMMARY_TOKENS = 400`, so a summariser that ignores its prompt cannot grow the payload.
- **The tokenizer is vendored (Phase 33).** `cl100k_base` ships at `vendor/tiktoken/<sha1>`;
  `token_budget.install_cache_dir()` points `TIKTOKEN_CACHE_DIR` at it at boot, so the count is exact
  and offline (a fallback warns once on stderr).
- **The context is not linear (Phase 31).** The payload is the immutable system prompt, a **rolling
  summary** of the older turns, and the last `CONTEXT_WINDOW_TURNS = 4` turns verbatim. Past
  `COMPRESS_AFTER_TURNS = 5`, `_compact` folds the rest with an injected `summarizer` (the cheapest
  configured slot, `tier_0`); no summarizer falls back to a FIFO drop plus an explicit
  `TRUNCATION_NOTICE` — sticky, so the model is told on every later turn. The original user message
  is pinned and never compressed.
- **The bill is bounded (Phase 32).** `tools/token_budget.py` counts each call (tiktoken when an
  encoding loads offline, else the documented estimate); `IntentLedger` accumulates
  `prompt_tokens`/`completion_tokens`; `TokenBudgetExceeded` is checked **before every LLM call and
  every tool execution** against `MAX_INTENT_TOKENS = 250000` (configurable via
  `ALETH_MAX_INTENT_TOKENS`).
- **The Node syntax check batches its arguments (Phase 32).** `find ... -exec node --check {} +`
  with the build-output directories pruned, not a shell glob: expanding a compiled frontend into one
  argv is how the check fails with E2BIG on the repository it protects.
- **One runtime per project, not a monolith (Phase 30).** `docker/sandbox.Dockerfile` is the Python
  toolchain and carries no other language; `tools.project_phases.detect_runtime` reads the project's
  manifests and `detect_image` picks `aleth-sandbox:latest` or `node:20-alpine` (pulled on first use
  by `pull_image`). A **polyglot** project is refused rather than guessed and declares its image in
  `.aleth_phases.json`. `run_isolated(..., image=…)` takes the resolved tag.
- **The merge gate has no free pass (Phase 30).** `verified=None` used to merge; now only
  `verified is True` merges. A project with no suite gets a **structural syntax check**
  (`python -m compileall -q .`, or `npm run build` / a `node --check` sweep skipping
  `node_modules`), and an undeterminable runtime is a *failure*, not a pass.
- **The egress phase is bounded (Phase 30).** `DEFAULT_SETUP_TIMEOUT_SECONDS = 120`, clamped to a
  600 s ceiling a human may raise in the declaration. A timeout kills the container and its network.
- **Setup runs with egress before the model is involved (Phase 29).** `tools/project_phases.py::run_setup`
  installs the project's dependencies from its own manifests (or a human's `.aleth_phases.json`),
  deterministically, in a container the model never gets. A failed install **refuses the run**. There
  is no `docker network disconnect` because there is no long-lived container: one container per
  command, `--rm`, so the setup namespace dies with it.
- **A failing suite makes the shadow unmergeable (Phase 29).** `run_verification` runs the project's
  suite **airgapped** over the finished shadow; a failure fails the intent and writes
  `verified=false` into the staging manifest, so `workspace_merge` refuses it. `verified=None` means
  "no suite declared", and a rejection is unaffected.
- **`ALETH_STATE_ROOT` replaces the descriptor baton (Phase 29).** `storage/connection.py` derives
  and publishes the absolute state directory **without overwriting** (a child cannot clobber what it
  inherited), `published_state_root()` is what a worker reads, and a relative value is refused.
  `publish_state_root()` -- the forced form -- runs only where the pointer *moves*: `set_plan_dir`,
  `set_project_dir`, and test fixtures.
- **The sandbox image carries node (Phase 29).** `npm install` / `npm test` are the setup and verify
  commands for a JavaScript project; without the toolchain the two phases would be a no-op for it.
- **The correlation id is thread-safe (Phase 28).** `tools/run_context.py` is a
  `contextvars.ContextVar`, not a module global: with a thread per run and a `threading.excepthook`,
  a global meant thread B's id filed thread A's crash. Anything off the run's thread asks
  `IntentQueue.current` instead — `stop_execution` runs on the API thread, where a context variable
  is invisible by design.
- **The supervisor records its children's deaths (Phase 28).** `Swarm._handle_failure` writes
  `kind=WORKER_TERMINATED` / `detail="Worker terminated unexpectedly"` joined to the tick's intent,
  captured into the `Future` callback at dispatch. A dying child cannot write its own autopsy.
- **One configured SQLite connection (Phase 28).** `storage/connection.py` owns
  `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` and `foreign_keys=ON`; all eight
  call sites use it. The default rollback journal locks the whole file for a write, which turns a
  concurrent poll into `database is locked` — a crashed engine rather than a slow one.
- **Egress is severed for good (Phase 28).** The plan-declared `net` capability and its
  `--allow-network` flag are **gone**; `SANDBOX_NETWORK = "none"` is a constant and the builders
  have no parameter for it. A plan that still declares `net` is told it has no effect. This reverses
  Phase 12's opt-in — see `MASTER_CONTEXT.md` §2.
- **A fault is joined to the intent that caused it (Phase 27).** `intent_id` is threaded from
  `_launch_run` through the registry, the runner, `WorkflowContext`, the planner and into
  `run_tool_loop` — and across the process boundary into swarm children, riding the role descriptor
  (`Swarm.tick(intent_id=…)` → `role_payload` → `worker.execute_node`), because a child cannot look
  it up. `agent_faults.intent_id` is `NOT NULL`, `record_agent_fault` **refuses a blank one**, and
  the table's Phase-26 `session_id` is dropped by a guarded migration.
- **An abort is durable, and the workflow layer reads it (Phase 27).** `IntentLedger.is_live` +
  `IntentLedger.abort`; `stop_execution` writes the abort. `run_tool_loop` checks liveness **before
  every step and before every tool call** and raises `RunAborted`, so a tool that mutates cannot
  overtake an abort that landed while the model was thinking — and no telemetry is written for a
  run that is already over.
- **A crash reports its intent (Phase 27).** `tools/run_context.py` holds the process's current
  intent and `install_exception_logging` (called first in `main.py`) installs both `sys.excepthook`
  and `threading.excepthook` to print `intent=<id>` on the same line as the failure, keeping the
  full traceback.
- **The shutdown sweep is scoped to the engine's own process tree (Phase 26).** Phase 25's version
  matched the `aleth.managed` label alone, so an engine exiting killed the in-flight containers of
  *any* other engine on the host. `purge_managed_containers` now checks each candidate's owner with
  `_is_our_child`, which walks `/proc`'s parent chain. **Descendants, not `os.getpid()` alone:** the
  engine never creates a container itself — `run_isolated` runs inside the exec server *child* — so
  a sweep matching only its own pid would remove nothing.
- **The model has a step ceiling, and hitting it is a fault (Phase 26).** `MAX_AGENT_STEPS = 30` in
  `orchestration/workflow/agent_loop.py`; exhausting it records a row and raises
  `AgentStepLimitExceeded("Exceeded maximum execution steps")`. It used to return an empty answer,
  which is a blank plan that looks like a decision. `planner._from_llm` re-raises it explicitly so
  the broad `except Exception` cannot degrade it to "no plan", and the run fails the intent with
  that message.
- **Every tool result is capped at 16 KB (Phase 26).** `tools/result_budget.py`;
  `mcp_fs_server.read_file` and the exec server's stdout/stderr block both go through it.
  Middle-truncated — a head-only slice would cut away the traceback — with the dropped byte count
  stated so the model reaches for `grep`. `execution_io.read_source` is deliberately exempt: its
  caller writes the content back.
- **The engine cleans its own containers on the way out (Phase 25).** `purge_managed_containers`
  removes every `aleth.managed` container **it is responsible for** — scoped in Phase 26 to its own
  process tree — and `install_shutdown_sweep` arms it on `atexit`
  **and** SIGINT/SIGTERM, re-delivering the signal so the process still exits with the expected
  status. It asks the daemon, because a container is a child of `dockerd` and process-group
  scoping never reached it.
- **The uncalled writer is gone (Phase 24).** `orchestration/workflow/actions_impl._write_checked`
  had no production caller — `executor.apply_artifact` writes through the **MCP filesystem server**
  (`client.write_file`), whose root is the shadow. It and `WorkflowWriteGuardTests` are deleted:
  code that does not run in production is a liability waiting to be wired up incorrectly.
- **The last non-atomic state write is closed (Phase 24).** `tools/workspace.py::_mint_identity`
  wrote `.aleth_id` with a truncating write. `_identity_from_file` accepts any prefix of the token
  that matches `_SAFE_ID_RE`, so a kill mid-write would not fail validation — it would answer as a
  **different project** and orphan the whole state directory silently. It now goes through
  `tools.atomic_io`. `_ignore_identity_file`'s append is deliberately left alone (harmless,
  self-correcting).
- **The container reaper is no longer killable by the kill it exists for (Phase 24).** The exec
  server's SIGTERM handler removed its in-flight containers with `docker rm -f` — a child of the
  very process group the MCP client SIGKILLs when its 5 s grace expires. The removal's own budget
  is 15 s, so under load the SIGKILL landed inside it and the container outlived its server. The
  removal now runs in **its own session** (`remove_container(detached=True)`, output to `DEVNULL`),
  so it completes regardless. This is the failure that made CI red on `ubuntu-latest / py3.11`;
  see `MASTER_CONTEXT.md` §4 for the full account.
- **The run's file I/O and the frontend's reads are two modules (Phase 23).** Phase 20 said
  reads resolve the user's tree and execution writes resolve the shadow, but both halves sat behind
  one resolver in `tools/workspace_io.py` — and `overwrite_source`/`read_source` resolved against
  the **project** directory. `tools/execution_io.py` now owns the run's half: it imports
  `get_execution_dir` and **cannot** import `get_project_dir`. `tools/workspace_io.py` keeps the
  frontend's reads (listing, preview, `.env`) and cannot reach the execution root.
  `tools/file_tools.py` re-exports both halves, so no import site moved.
- **`read_source` was the live defect (Phase 23).** `fix_bug` and `next_step` read the host while
  the patch was applied to the shadow over MCP, so a patch was computed against a stale base — a
  second step could not see the first step's edit. `overwrite_source` was latent, and Phase 24
  deleted its only (uncalled) caller, `actions_impl._write_checked`. Both are bound correctly now.
- **The root is injected, not guessed (Phase 23).** `WorkflowContext.execution_root` is captured
  from the shadow when the run starts and threaded into the writer and the read-modify-write reads;
  `tools.execution_io._resolve` raises a hard `PathDenied` (**a `ValueError`**) for any path outside
  the granted root, so an injected root cannot be escaped with a filename.
- **An agent can no longer write to the user's live tree.** Every run copies the
- **Every durable write lands whole or not at all (Phase 22).** `tools/atomic_io.py` writes a
  temporary **sibling of its target** and then `os.replace`s it. The sibling is not a style
  choice: `os.replace` is `rename(2)`, atomic only *within one filesystem* and `EXDEV` across
  two — and the shadow (`~/.aleth/state/<project>/staging`) and a merge's destination (the user's
  repository) are frequently two mounts, with `/tmp` a third under WSL. The temporary's name
  carries the `.tmp` prefix the engine already treats as scratch (`staging._copy_ignore` prunes
  it from a shadow walk; `workspace_io.list_workspace_files` skips it), and every failure path
  unlinks it, so a SIGKILL mid-write leaves no litter to mistake for a project file.
- **The write sites it replaced.** `mcp_fs_server.write_file` truncated in place — the vector the
  directive named; `staging._apply_one` used `shutil.copy2`, which writes *into* the user's own
  inode, so a kill mid-merge left half a file where their uncommitted work was; `staging`'s
  manifest; the three `plan_state` writers (`PLAN.md` is git-tracked); `ast_editor`'s splice;
  `workspace_io.overwrite_source`; and `recovery`'s revision snapshot, task snapshots,
  `_meta.json` and rollback restore. `settings._rewrite_env` and `prompt_editor` (the two writers
  of the engine's own source/`.env`) went too, on the same rule rather than as exceptions.
- **The engine's own state has a bound (Phase 22).** `storage/retention.py::sweep_state` is wired
  into `main.py` **immediately after** the container sweep and before the bootloader. It
  reconciles the intent ledger **first** (so a killed run's shadow is collectable on that boot,
  not the next), trims `execution_telemetry` / `routing_decisions` / `intent_ledger` to their
  newest `LEDGER_KEEP_ROWS = 1000` rows with one `rowid`-keyed `DELETE` per table (each answered
  by the recency index already on it — no ORM, no migration framework), and calls
  `staging.purge_orphaned_stagings` with the ledger's own answer to which shadows are still
  owned.
- **Which shadows survive (Phase 22).** One whose intent is `queued`, `running` or `completed` —
  in use, or waiting to be reviewed and merged. Everything else is collected, including a
  directory whose manifest a kill prevented from ever being written. An intent that `failed` or
  was `stopped` can never be merged (the gate refuses an unfinished plan), so its copy is
  unreachable by every endpoint in `api/operations.py`.
- **The sweep is loud and cannot break a boot.** It reports what it collected, zeros included,
  because a silent collector hides the crash that produced the garbage; and `sweep_state` never
  raises — a failure is reported as `skipped` and boot continues.
- **An agent can no longer write to the user's live tree.** Every run copies the
  workspace into a shadow under `<state_root>/staging/<staging_id>` (`tools/staging.py`);
  the agent's MCP servers and the container bind-mount that copy. `tools/workspace.py`
  now has **two roots**: `get_project_dir()` for reads (plan, explorer, preview, source
  spans, the agent listing) and `get_execution_dir()` for execution writes (the fs
  server, the container mount, `execute_approved`'s apply target, the planning context,
  `test_runner`'s cwd). `EngineService._staged_execution` sets the execution root for the
  duration of the intent; a nested call reuses the active shadow, so the autonomous
  loop's passes accumulate into one copy.
- **The copy is the project as git sees it (Phase 21).** `create_staging` filters the
  copy with `git ls-files --cached --others --exclude-standard`, so a virtualenv, a
  `node_modules` tree and a build output never enter a shadow. The ignore rules are
  pushed down to **git**, not re-derived in Python and not delegated to `rsync`; a
  non-repository workspace falls back to a walk pruned by `IGNORE_DIRS`. The host side of
  a diff is filtered the same way, so an ignored file is never reported as deleted.
- **A shadow answers to exactly one execution id (Phase 21).** `find_staging` resolves by
  `staging_id` or `intent_id` and **never** by plan; the plan fallback is deleted. The
  diff/merge requests model `intent_id: str` (required), and a blank id is refused, so an
  orphaned shadow is dead by construction. `_staged_execution` mints an id when none is
  given and reuses a run's existing shadow, so a manual approval joins the delta it releases.
- **Merges are serialised (Phase 21).** `tools/staging.py::merge_lock()` is a per-project
  `filelock` (`<state_dir>/merge.lock`) held across the gate check **and** the apply,
  acquired non-blocking. A concurrent merge raises `StagingLocked`, which
  `api/gateway.py` maps to a **409** (its one service-exception translation). The UI
  remembers the run's id (`state.activeIntentId`) and sends it with `approve_artifact`.
- **The delta leaves only through a merge gate.** `GET /api/workspace/diff` returns the
  content-addressed delta (added/modified/deleted + a capped unified patch);
  `POST /api/workspace/merge` applies it and purges the shadow, or (approve=false)
  discards it. A merge is **refused** unless `autonomy.plan_progress(...).finished` —
  every task completed. Both are rows of `api/operations.py` **and** entries in
  `ui/js/api-client.js` (the parity test pins the two tables 1:1).
- **A pre-existing isolation defect was fixed, not excused.** `env_boot` loads `.env`
  with `override=True` at `app` import, so a test asserting a committed routing default
  was order-dependent. `tests/conftest.py::_no_ambient_routing_overrides` clears the
  three `ALETH_*_MODEL` names around every test — the analogue of the `LAYA_BACKEND`
  injection from Phase 19.
- **Agent routing is a typed gateway.** `orchestration/routing.py::classify_task` picks
  System 2 (the Architect) vs System 1 (a Coder). It **never raises**: a classifier
  failure or a malformed verdict routes to the Architect with `CLASSIFIER_FAULT`
  (logged `CRITICAL`). The classifier is injected; the default is `agents.laya_model.classify`.
- **Every routing decision is audited.** `storage/telemetry.py::routing_decisions` is its
  own append-only table. `Swarm._descriptor_for` classifies on the **parent** side of the
  pool boundary, records the decision, and ships it to the child in the descriptor.
- **The model tier is unchanged.** `orchestration/model_router.py` still owns the model
  choice (Phase 7); the Phase 19 band is a second, independent input. Do not collapse
  the two authorities.
- **Corrections to stale notes below.** `plan.json` no longer exists — machine state is
  `aleth_state.db` under `~/.aleth/state/<project_id>/` (Phase 11); `PLAN.md` stays in the
  repository. The project is `aleth`, not `deepagents`, except the `deepagents` PyPI
  framework and the `deepagents_core` Rust crate, which keep their names.

---

## 📌 1. Executive Summary & Objective

**Project Name:** Aleth Desktop UI & Multi-Agent Orchestrator Studio
**Primary Goal:** A native desktop application built with **Python (`pywebview`)**,
**HTML5/CSS3**, and **vanilla JavaScript**, styled as an off-code development IDE:
neutral high-contrast dark greys, 1px pane borders, monospaced technical data, and
colour reserved for state rather than decoration. It coordinates multi-agent software
engineering workflows anchored in a dynamic markdown plan file (`PLAN.md`); its machine
state lives outside the repository, in `~/.aleth/state/<project_id>/aleth_state.db`
(the legacy `plan.json` twin was removed in Phase 11).

### The Problem It Solves

1. Eliminates multi-turn LLM context window explosion by enforcing **stateless
   sessions per prompt**; project memory is stored exclusively on disk in the
   dynamically selected `.md` plan file.
2. Eliminates rogue architectural behavior by enforcing **mandatory delegation**: the
   Lead Architect plans, verifies, and delegates, but **NEVER writes application
   code**. Only Coder sub-agents write code and execute arbitrary shell commands.
   Administrative intents (plan edits, analysis, recommendations) short-circuit to the
   Architect with **zero Coder spawns**.
3. Provides real-time visibility through a **four-pane IDE layout**: a tabbed left
   sidebar (Agents / Plans / Files / Env), a centre **Dual-View Workbench** that hosts the
   plan document *and* the plan tracker (bento header + roadmap cards), the polymorphic
   per-action **result view**, and a resizable bottom dock holding the **command drawer**
   and the run-scoped console — all driven by a **zero-token native markdown parser**
   (`tools/plan_parser.py`) rather than an LLM round-trip.

---

## 🗂️ 2. Repository & File Structure

```
aleth/
├── app.py                     # pywebview host: window, BridgeAPI (the JS-facing API), worker thread
├── main.py                    # Entrypoint: desktop UI by default, headless with --cli
├── registry.py                # AgentRegistry facade (~158 lines) — delegates to orchestration/
├── env_boot.py                # Loads .env; a shadowed export is reported, not silently used
├── HANDOFF.md                 # Master handoff document (this file)
│
├── agents/                    # Agent definitions. These double as DATA FILES:
│   │                          #   the prompt editor rewrites them on disk at runtime.
│   ├── __init__.py            # Exports architect_agent, coder_deep, coder_standard
│   ├── architect.py           # software-architect: core prompt + custom directives
│   ├── coders.py              # coder-deep and coder-standard worker definitions
│   ├── laya.py                # System 1: the zero-token heuristic classifier (`classify`)
│   ├── laya_model.py          # System 1's env-gated ModernBERT backend (same seam)
│   └── model_routing.py       # route names (`openai:policy/x`) + architect_model()/coder_model()
│
├── orchestration/             # Extracted from the former monolithic registry.py
│   ├── __init__.py            # Public re-exports of the modules below
│   ├── agent_catalog.py       # Reflection over agents/ -> the catalogue the UI renders
│   ├── plan_session.py        # Roadmap scaffolding + reading the active plan
│   ├── plan_tagging.py        # The offline [UI] re-tagging pass (Laya), background thread
│   ├── prompt_editor.py       # Persists edited prompts back into the agents/ sources
│   ├── system2.py             # THE one place a model request is made (vision-capable)
│   └── workflow/
│       ├── __init__.py
│       ├── runner.py          # Intent resolution, dispatch, error handling
│       ├── context.py         # WorkflowContext + the Line-Anchored Context Slicer
│       │                      #   (the slicer itself is in the compiled core)
│       ├── events.py          # THE single definition of the frontend event wire format
│       ├── reasoning.py       # The Architect's System 2 answer as streamed UI text
│       ├── ledger.py          # Living Behavioral Ledger: the 🟢 log line + state summary
│       ├── generation.py      # Plan slice -> one Coder deliverable (System 2 + fallback)
│       ├── actions_admin.py   # Administrative bypass: update_plan, analyze, recommend, normalize
│       ├── actions_impl.py    # Delegated implementation: fix_bug, next_step, custom
│       └── templates.py       # Deliverable templates (solution engine, ui_view, weather API)
│
├── tools/                     # Tooling package
│   ├── __init__.py            # Clean re-exports of the file tools
│   ├── file_ops.py            # @tool primitives, workspace listing, masked .env reader
│   ├── file_tools.py          # Compatibility facade that re-exports its siblings
│   ├── plan_parser.py         # Thin wrapper over the compiled core (deepagents_core):
│   │                          #   markdown <-> plan dict, sub-steps, Global State Summary,
│   │                          #   [UI] detection, deliverables
│   ├── plan_state.py          # Thin wrapper over the compiled core: dual-sync
│   │                          #   plan.json <-> the active markdown plan, atomic writes
│   │                          #   under one OS lock; raises PlanWriteError on failure
│   ├── task_tags.py           # The tag vocabulary (UI_TAG) shared by parser & UI
│   ├── recovery.py            # Backups, codebase/plan audit, sync resolution, rollback,
│   │                          #   and the plan-revision snapshot/revert
│   ├── code_metrics.py        # Static source metrics (complexity, security) for Analyze
│   ├── test_runner.py         # Runs the test a task wrote; the result view's real verdict
│   ├── git_status.py          # VCS status for the tree (snapshot fallback outside a repo)
│   ├── workspace.py           # PROJECT_DIR (ALETH_WORKSPACE_DIR) + PLAN_DIR pointers
│   ├── shell_tools.py         # Full shell (Coders) + restricted shell (Architect)
│   ├── settings.py            # Provider endpoint + model-route settings (on disk)
│   ├── laya_bench.py          # Dev-only: Laya checkpoint I/O benchmark
│   ├── system2_cost.py        # Dev-only: measures what a prompt costs at the real API
│   ├── check_ui_state.py      # Dev-only: fails if a `state`/`DOM` field gains a second writer
│   └── (the plan engine and context slicer now live in crates/deepagents_core)
│
├── crates/deepagents_core/    # THE COMPILED CORE (Rust, exposed to Python by PyO3)
│   ├── Cargo.toml             # pyo3, serde_json, pulldown-cmark, regex, fs2
│   ├── rust-toolchain.toml    # host-neutral channel; the machine selects GNU (see §3)
│   ├── .cargo/config.toml     # PYO3_USE_RAW_DYLIB=0 so PyO3 links the interpreter's .lib
│   ├── pyproject.toml         # maturin build definition; module name deepagents_core
│   ├── deepagents_core.pyi    # Type stubs for the boundary (shipped in the wheel)
│   └── src/
│       ├── lib.rs             # The Python-facing surface (every #[pyfunction])
│       ├── tokenizer.rs       # markdown-it-shaped token stream over pulldown-cmark
│       ├── parser.rs          # Markdown <-> plan dict, structure check, tag extraction
│       ├── state.rs           # Dual-sync, atomic writes, fs2 locking, plan lock file
│       └── text.rs            # Middle truncation + the Line-Anchored Context Slicer
│
├── ui/
│   ├── index.html             # Layout markup + the head watchdog + one module script tag
│   ├── css/                   # 19 stylesheets (source of truth), imported by main.js
│   │   ├── base.css                     # Design tokens (:root) + reset & base styles
│   │   ├── sidebar.css                  # Left pane: agent/plan/file/env tabs & workspace card
│   │   ├── stage.css                    # Centre stage & the execution cards
│   │   ├── actions.css                  # The docked command drawer & its parameter form
│   │   ├── plan-tree.css                # Workbench roadmap: bento header, cards, progress bands
│   │   ├── modals.css                   # Modal base + the workspace-files dialog
│   │   ├── tiered-prompt-editor.css     # Tiered prompt editor (core rules panel)
│   │   ├── ui-vision.css                # Multimodal UI vision parameters
│   │   ├── audit-modal.css              # Audit/summary rail & the plan panels it hosts
│   │   ├── rollback-modal.css           # Rollback modal & shared keyframes
│   │   ├── dock.css                     # Bottom dock geometry & the drag seam
│   │   ├── workbench.css                # Centre-stage plan document & the bento header grid
│   │   ├── code-surface.css             # Highlighting surface for parameter textareas
│   │   ├── console.css                  # Dock console transcript
│   │   ├── preview.css                  # Live preview overlay
│   │   ├── sidebar-panels.css           # Collapse rails + tree/env panels
│   │   ├── settings.css                 # Provider/model route settings panel
│   │   ├── command-palette.css          # Ctrl/Cmd+K action palette
│   │   └── result-view.css              # Polymorphic per-action result surface (appended last)
│   └── js/                    # 22 ES modules (source of truth), imported by main.js
│       ├── main.js            # The entry point: imports every module + the stylesheets
│       ├── store.js           # The store: `state`, the `DOM` cache, and the named
│       │                      #   operations for the fields more than one module writes
│       ├── dom.js             # Element lookup helpers + escapeHtml()
│       ├── bootstrap.js       # pywebview handshake, opt-in ?demo=1 data, hydration
│       ├── plan-tree.js       # Roadmap from plan.json: bento header, phase cards,
│       │                      #   step cards, sub-step rows, progress meter
│       ├── actions.js         # Action drawer open/close + confirm, execution lifecycle
│       ├── plan-modals.js     # Switch-plan and create-plan rail panels
│       ├── agents.js          # Sidebar agent navigation & the system prompt editor
│       ├── workspace.js       # File browser, codebase-sync audit, rollback modals
│       ├── agent-events.js    # window.onAgentEvent: the inbound event stream renderer
│       ├── visuals.js         # Toast notifications & the offline workflow simulator
│       ├── dock.js            # Bottom dock resize + the drawer's open/close pane state
│       ├── workbench.js       # Plan Workbench: the active plan in the centre stage
│       ├── code-surface.js    # Syntax highlighting for the parameter textareas
│       ├── console.js         # Dock console: terminal-style echo of the agent stream
│       ├── preview.js         # Live preview of the workspace's generated interface
│       ├── sidebar.js         # Collapsible side panes & the workspace file tree
│       ├── env.js             # Environment variable panel (values masked in Python)
│       ├── settings.js        # Provider/model route settings panel
│       ├── command-palette.js # Ctrl/Cmd+K action palette + its key handling
│       ├── result-view.js     # Polymorphic per-action result renderers (diff, dashboard…)
│       └── wire.js            # DOM event wiring & bootstrap — imported last by main.js
│
├── dist/                      # THE SERVED FRONTEND: `npm run build` output
│                              #   (index.html + one JS asset + one stylesheet; gitignored)
│
├── tests/                     # pytest (xdist) + Playwright
│   ├── test_characterization.py  # Behavioral-equivalence harness (the bulk of the suite)
│   ├── test_laya.py              # System 1 heuristic classifier
│   ├── test_laya_model.py        # The env-gated ModernBERT backend
│   ├── test_generation.py        # Plan slice -> deliverable; the System 2 seam + vision input
│   ├── test_system2.py           # The provider seam (config, routing, failure -> None)
│   ├── test_plan_tagging.py      # The offline [UI] re-tagging pass
│   ├── test_context_slicer.py    # Line-Anchored Context Slicing
│   ├── test_architect_reasoning.py / test_ledger.py / test_code_metrics.py
│   ├── test_settings.py / test_task_tags.py / test_test_runner.py
│   ├── test_git_status.py / test_normalization_gate.py / test_env_boot.py
│   ├── _plan_prebak.md           # The PREVIOUS plan's last copy — do not delete
│   └── ui/                       # Playwright: harness.mjs, structural.spec.mjs,
│                                 #   interactions.spec.mjs (26 tests, 17 behavioural)
│
├── package.json               # vite + eslint + @playwright/test; scripts: build, dev,
│                              #   preview, lint, test:ui
├── vite.config.mjs            # ui/ -> dist/ (plain ES-module build, source maps on)
├── eslint.config.mjs          # no-undef / no-redeclare / no-unused-vars over ui/js
├── playwright.config.mjs      # serves dist/ with `vite preview`, tests tests/ui/
├── pytest.ini                 # testpaths + `-n auto` (parallel workers)
│
├── PLAN.md / plan.json        # THE ACTIVE PLAN lives in the PLAN DIR == this repo root
│                              #   (PLAN.md is tracked; plan.json is gitignored)
├── 1ST/2ND/3RD_POLISHING.md   # The three polishing plans + their progress logs. Listed in
│                              #   NON_PLAN_MD_FILES, so the switcher never offers them.
│
├── my_project_workspace/      # RETIRED location of PROJECT_DIR (now ALETH_WORKSPACE_DIR)
│   ├── main.py / weather_api.py / ui_view.html   # Legacy artifacts of the earlier weather plan
│   ├── test_*.py / PROGRESS.md
│   └── .aleth_backups/   # Per-task deliverable snapshots (rollback source),
│                              #   plus plan_revision/ — the before-image the result view's
│                              #   "Revert Changes" restores from
│
└── venv/                      # Python 3.12 Virtual Environment
```

`main.py` and `app.py` both import the module named `registry`, which is why the
package that registry.py delegates to is called `orchestration/` — a package named
`registry` would shadow the module.

**Two directories, do not conflate them.** `tools/workspace.py` owns both pointers:
`PROJECT_DIR` = `ALETH_WORKSPACE_DIR` (default `~/workspaces/my_project`) — the **sandbox**
that generated code, deliverables and backups land in, and a path *in the daemon's filesystem*,
because a container bind-mounts it — and `PLAN_DIR` = the **repo root** (where `PLAN.md` and
its `plan.json` twin live, so the roadmap is version-controlled). A plan edit therefore
changes a tracked file in the repo root, while generated code lands in the sandbox. The
gitignored `my_project_workspace/` beside the repository is the **retired** location: a
container writing through a Windows drive destroys the host's permissions, so the workspace
moved onto the daemon's filesystem and its contents were copied to `~/workspaces/my_project`.

---

## ⚙️ 3. Environment & Quick Launch Commands

All Python dependencies are installed in the local virtual environment (`.\venv`).
Node/npm are used for the frontend build (`vite`) and the UI tests (`@playwright/test`).
The plan engine and the context slicer are a compiled Rust extension; see §3.1.

```sh
# 0. Build the frontend once (the app serves dist/, not ui/):
npm install && npm run build

# 1. Launch the native desktop application (pywebview):
./venv/Scripts/python.exe app.py

# 1b. Same, with WebView2 DevTools open (diagnose a window that renders but
#     ignores input: the console and network log show which request or script failed):
./venv/Scripts/python.exe app.py --debug

# 2. Run headless multi-agent verification (CLI mode):
./venv/Scripts/python.exe main.py --cli "Implement weather service endpoints"

# 3. Test imports & syntax across the project:
./venv/Scripts/python.exe -c "import app, registry, tools, agents; print('All systems operational!')"

# 4. Verify the plan tree native parser (zero LLM tokens):
./venv/Scripts/python.exe -c "from tools.file_tools import read_file, parse_plan_tree; print(parse_plan_tree(read_file.invoke({'filename': 'PLAN.md'})))"
```

The **shell is `sh`** (Git Bash / the agent's terminal), so use forward slashes:
`./venv/Scripts/python.exe`. A PowerShell session would use `.\venv\Scripts\python.exe`;
the two are otherwise identical.

### 3.1 The compiled core (`crates/deepagents_core`)

The plan parser, the dual-sync state engine and the context slicer are Rust, exposed to
Python by PyO3 and built by maturin. `tools/plan_parser.py`, `tools/plan_state.py`,
`orchestration/workflow/context.py` and the three truncation sites (`tools/file_ops.py`,
`tools/shell_tools.py`, `tools/test_runner.py`) are thin wrappers over it.

```sh
# Rebuild the extension after ANY change under crates/ (installs into ./venv):
cd crates/deepagents_core
export VIRTUAL_ENV="C:\\Users\\muham\\OneDrive\\Desktop\\aleth\\venv"
../../venv/Scripts/maturin.exe develop --release

# Rust unit tests (tokenizer, parser, state, slicing):
cargo test
```

Three environment facts, all pinned in the crate so a plain `cargo build` works:

- **The toolchain file names a host-neutral channel** (`rust-toolchain.toml`, `channel = "stable"`).
The target is a property of the host, not of the repository: rustup refuses a fully-qualified
channel (`stable-<triple>`) unless that exact toolchain happens to be installed, so a pinned one
builds on exactly one machine. This machine has the MSVC compiler but not the Windows SDK's
import libraries, so an MSVC link fails with `LNK1181: cannot open input file 'kernel32.lib'`;
its rustup default is therefore the self-contained GNU toolchain
(`rustup default stable-x86_64-pc-windows-gnu`), which ships its own linker and needs nothing
installed.
- **`PYO3_USE_RAW_DYLIB=0`** (`.cargo/config.toml`). PyO3 0.29 defaults to `raw-dylib`, which
makes rustc synthesise an import library with `dlltool` at link time; that fails here. The
opt-out links the interpreter's own `python3XX.lib` instead.
- **`python312.lib` comes from the base CPython install**, not the venv -- a venv has no
`libs/` directory. `maturin develop` finds it through the interpreter's `sys.base_prefix`.

### The verification gates (run all of them after any change)

```sh
# A. Lint the frontend AND its dependency graph: `no-undef` catches a reference that was
#    never imported (the one fault the ES-module architecture can produce that the build
#    itself does not), and the same command runs dependency-cruiser, which fails on any
#    cycle in `ui/js`. Reintroducing a back-edge is a lint error, not a review catch.
npm run lint

# B. Build the frontend. Everything after this serves or tests the artifact it produces,
#    so it comes first: `npx playwright test` runs against `dist/`, not against `ui/`.
npm run build

# C. Frontend behaviour, in a real browser. 26 tests: the shell and the plan tree, the dock
#    resize seam, the command palette, the inbound agent wire, every action's result view.
npx playwright test

# D. The shared-state invariant (fails if a `state`/`DOM` field gains a second writer):
./venv/Scripts/python.exe tools/check_ui_state.py

# E. Backend suite: 408 tests, run in parallel by pytest-xdist.
./venv/Scripts/python.exe -m pytest

# E2. The same suite serially, when a failure looks like a parallel-execution artefact:
./venv/Scripts/python.exe -m pytest -n0

# F. The Rust core's own tests:
cd crates/deepagents_core && cargo test
```

Any `ui/js` or `ui/css` edit **requires** gates A and B before gate C — the lint is what
catches a missing import, the build is what turns the sources into the artifact the app
loads, and the Playwright suite is what proves that artifact still runs.

Expected non-failures in the output, none of which is a test failure:
`[DualSync] Error reading plan.json: ...` (a workspace with no machine state yet, **and**
the `test_a_corrupt_plan_json_is_reported_not_hidden` case), `[BridgeAPI] Detached console
failed: no gui`, and the two `[System2]` lines from the mocked-provider tests.

**This repository is synced by OneDrive**, which takes short exclusive handles on files
here. The workspace suite is built so that it never needs to fight that: it points the
project and plan pointers at a throwaway directory, restores them by assigning the module
globals rather than by calling the setters (the setters re-hydrate, which would rewrite the
developer's `plan.json` on every test), and only writes a snapshot back when the bytes
actually changed. A full run therefore leaves `PLAN.md` and `plan.json` byte-identical.
If a parallel run ever fails on a file operation, re-run `-n0` before suspecting the code.

### Frontend change protocol (hard rules)

`ui/js/*.js` and `ui/css/*.css` are the source of truth. They are **ES modules**: each one
imports what it uses and exports what it provides, `ui/js/main.js` is the single entry point
that imports them in evaluation order, and `npm run build` (Vite) bundles the graph into
`dist/` — one JS asset and one stylesheet. The app loads `dist/index.html` through a WebView2
virtual-host mapping (`app.py`, `ASSET_HOST`), so the document and its assets are ordinary
https requests resolved out of the folder.

Therefore, after editing any `ui/js` or `ui/css` file:

1. `npm run lint` — `no-undef` catches a reference that was not imported, and the
   dependency-cruiser `no-circular` rule fails on a new cycle. Both are faults this
   architecture can produce that nothing else sees at build time. Break a cycle by
   publishing an intent on `ui/js/bus.js` (see §8 bug 22); do not weaken the rule.
2. `npm run build`.
3. `npx playwright test` — 26 behavioural tests, and they fail loudly if the bundle did not
   execute or a path threw.

Additional invariants:

- **Add a module by importing it.** There is no module list to keep in step: a new file is
  reachable once something imports it, and `main.js` is where the app's own modules are
  named. Order matters only for side effects at evaluation time — `wire.js` registers the
  `DOMContentLoaded` bootstrap that calls into everything else, so it stays last in
  `main.js`. Stylesheets are imported in `main.js` in cascade order, and a new one appends
  last there.
- **A module may only touch what it imports.** `state` and `DOM` live in `ui/js/store.js` and
  are imported from it; they are deliberately not on `window` any more. If you need a new
  shared field, add it to the store's object and export it — do not reach for a global.
- **A `state` field written from more than one module must go through a named store
  operation.** That is the rule that keeps "who changes this?" answerable; `python
  tools/check_ui_state.py` fails if a field gains a second writer. Reads stay direct.
- **The head watchdog no longer names 35 entry points.** It checks one thing: that
  `window.Aleth` exists, i.e. that the bundle executed at all. Everything finer-grained
  is reported from inside the module graph, where the names are visible: `runStartupStep`
  names a failed wiring step, and the wiring reports any cached element that did not resolve.
- **`window.Aleth` is the only thing this bundle publishes.** It carries the boot
  marker and `diagnostics()` (the app's own DOM-cache health report, which the Playwright
  suite asserts on). Do not add a second global; add to that object or, better, import.
- A failure in a startup step is reported as a visible banner — that is intended behaviour,
  not a bug. The Playwright suite asserts the banner is absent.

> `ui/index.html` is hand-authored markup plus one `<script type="module">` tag; `dist/` is
gitignored and rebuilt, never edited.

---

## 🏛️ 4. Core Architecture & Two-Tier Agent Taxonomy

Agent definitions are never hardcoded inside UI views. `registry.AgentRegistry`
delegates catalogue building to `orchestration/agent_catalog.py`, which reflects over
`agents/` and categorizes the result into two distinct tiers:

### Tier 1: Main Agents (Coordinators)
- **Agent ID:** `software-architect` (Lead Software Architect)
- **Implementation:** compiled state graph / coordinator (`agents/architect.py`).
- **Permissions:** **Restricted Shell Access** (`execute_restricted_command`, for
  `py_compile`, `pytest`, verification only).
- **Absolute Rule:** the Architect is strictly a planner, verifier, and delegator. It
  **NEVER writes application code** or creates files directly. It **ALWAYS delegates**
  implementation to Coder sub-agents. File writes are limited to the active plan file
  and `plan.json`.
- **Administrative Bypass:** plan edits, codebase analysis, and recommendations are
  executed by the Architect directly, with zero Coder spawns and zero Coder tokens.

### Tier 2: Coder Agents (Sub-Agents)
- **Agent IDs:**
  - `coder-deep` (Senior Backend Coder): complex logic, algorithms, API microservices.
  - `coder-standard` (Junior Developer): scaffolding, boilerplate, unit tests, docs.
- **Permissions:** **Full Shell Access** (`execute_shell_command`) + full file writing.
- **Standardized logging protocol (Living Behavioral Ledger):** completion is recorded on
  the task itself, **never** as a new `##` section. The Coder's own prompt
  (`agents/coders.py`) asks for **one** `🟢 Behavioral Log:` line beneath the task's
  `- [x]` checkbox:

  ```markdown
  - [x] Build the forecast endpoint
    - 🟢 Behavioral Log: added app/api.py with the forecast route and a paired test.
  ```

  The workflow adds the machine-owned lines beside it — `Deliverables: ``a.py``, ``test_a.py`` `
  on completion, `Verification failed: …` on a failed gate — and writes the task's
  `files[]` directly. The parser does **not** infer `files[]` from prose: a
  `Files Created/Modified:` line is kept verbatim as a `details` entry only (pinned by
  `test_deliverables_are_kept_as_raw_detail_text`).

---

## 🔄 5. End-to-End Execution Flow

The old free-text input bar and its "Are you sure…?" confirmation modal are gone.
Execution is intent-first: the **command palette** (Ctrl/Cmd+K, or the top bar's
`Commands` button) is the action entry point — the six-button dock strip was removed in
Wave 4. Choosing an intent opens the **command drawer**, a pane of the dock, to collect
that action's parameters (target task, vision mockup, bug evidence, custom directive).
Confirming the drawer is the confirmation step.

```
[Command Palette]  (Ctrl/Cmd+K — the six intents the dock strip used to hold)
      │  fix_bug │ next_step │ update_plan │ analyze │ recommend │ custom
      ▼
[Command Drawer]  (openActionDrawer(actionType, extraParams)) — a pane of #bottomDock
      │  User reviews the targeted task / supplies the directive, then confirms
      ▼
[BridgeAPI.start_execution(user_message, action_type, action_params)]  (app.py)
      │  SERVER-SIDE RUN LOCK: refuses while a run is live (the client lock can be
      │  defeated by a reload). Otherwise spawns a daemon worker thread; emit_event ->
      │  window.evaluate_js
      ▼
[AgentRegistry.run_agent_workflow]  (registry.py -> orchestration/workflow/runner.py)
      │
      ├── resolve_intent(): an explicit action_type is never overridden; only
      │      "custom" is auto-detected from an [ACTION: ...] tag in the message.
      ├── load_plan_state() + get_active_plan_filename()
      ├── emit "workflow_started", then "architect_spawn"
      │
      └── Dispatch:
             update_plan -> actions_admin.update_plan_action    (no Coder spawn)
             analyze     -> actions_admin.analyze_action        (no Coder spawn)
             recommend   -> actions_admin.recommend_action      (no Coder spawn)
             normalize   -> actions_admin.normalize_plan_action (no Coder spawn)
             fix_bug     -> actions_impl.fix_bug_action         (snapshot key "bugfix")
             next_step   -> actions_impl.next_step_action       (snapshot key = task id)
             custom      -> actions_impl.custom_action          (snapshot key "custom")
```

Two other runs do not come from the palette but take the same lock and Stop:
`normalize_plan` (the Normalization Gate's "Format via Architect") and
`retag_plan_with_laya` (the offline `[UI]` re-tagging pass — a background thread that
streams `laya_tagging_started` / `_progress` / `_done`). The Retry buttons call
`retry_execution`, which is `start_execution` under the same lock.

### The delegated (Coder) branch

```
1. Snapshot the task's deliverables into
   .aleth_backups/<key>/  together with _meta.json.
2. emit "delegation"      -> the right-pane tree pulses the target card
   emit "coder_spawn"     -> a Coder card slides in beside the Architect card
3. The Coder writes source and test files, ticks the task's checkbox in the plan,
   and appends its standardized progress entry.
4. emit "plan_updated"    -> applyPlanData() -> right-pane tree + progress meter
                             + the workbench document all re-render from the new
                             payload. This is the single funnel for plan data.
5. Architect verification: restricted shell (py_compile / pytest), then
   emit "architect_summary" with the approval and the proposed next steps.
6. emit "workflow_complete" -> the console settles, Stop hides, and the action's own
   **result view** mounts in the centre stage (unless the run was halted or errored — a
   halt keeps the transcript, a crash shows `Run Failed`). The Update Plan result offers a
   one-click **Revert Changes**, restoring the plan-revision snapshot taken before the
   write.
```

### The event wire format

Every payload reaches JavaScript through pywebview's `evaluate_js`, and the UI
switches on `type` in `ui/js/agent-events.js`. `orchestration/workflow/events.py` is
the single definition of this format — key order is part of the contract, because
payloads are JSON-serialised in insertion order.

| `type` | Meaning |
| --- | --- |
| `workflow_started` | A run began; carries the plan file and resolved intent |
| `architect_spawn` | The Architect card is created |
| `log` | One streamed line of agent narration (`log_type` drives styling) |
| `tool_call` / `tool_result` | A tool invocation and its paired outcome; animated as one unit |
| `delegation` | The Architect has chosen a Coder and a target task |
| `coder_spawn` | The Coder's card is created |
| `coder_summary` | The Coder's deliverable summary |
| `architect_summary` | The Architect's verification verdict and next steps |
| `plan_updated` | Plan data changed; the payload is the plan + its parsed tree |
| `agent_error` | A run failed |
| `workflow_stopped` | A stop request was honoured |
| `workflow_complete` | The run finished (`status` is `finished` or `error`) |
| `workspace_changed` | The active project directory changed |
| `agents_updated` | The agent catalogue changed (e.g. a prompt was saved) |
| `laya_tagging_started` / `_progress` / `_done` | The offline `[UI]` re-tagging pass; `_done` carries `changed` + `engine` |

> Payloads carry only what the UI reads. `can_retry`, the spawn-event `role`, and
> `workflow_started.message` were removed (audit L2); `delegation.task` stays because the
> console now renders the delegated task text.
>
> Do **not** add an `"exists"` key to `plan_updated` payloads — the characterization
> suite pins the exact wire shape in three places.

---

## 🖥️ 6. Desktop UI Specifications & Reference Aesthetic

The interface is an **off-code development IDE**: flat neutral dark greys in the
spirit of the VS Code default theme, sharp 1px borders between panes instead of drop
shadows, monospaced technical data (`--font-mono`), crisp sans-serif UI text
(`--font-ui`), and colour reserved for **functional state** only — blue `#0ea5e9` for
selection/primary, cyan `#06b6d4` for running, amber `#f59e0b` for warning/in-progress,
red `#ef4444` for stop/danger. There are no decorative gradients or ambient glows.

**Layout chain:**

```
body{height:100vh;overflow:hidden}
└── .app-layout{display:flex;height:100vh}
    ├── aside.sidebar#leftSidebar          (250px, collapses to a rail; 4 tabs)
    ├── main.main-stage
    │   ├── header.top-status-bar          (48px: Ready · active plan · completion · Stop · Commands · Preview)
    │   └── section.workspace-view#chatWorkspaceView   (column)
    │       ├── .stage-center              (workbench + execution stage + result view + preview)
    │       └── section.bottom-dock#bottomDock   (command drawer + console)
    └── aside.right-plan-sidebar#rightPlanSidebar   (fixed rail; collapses, never drags)
```

### 1. Left Pane — 250px, collapsible to a ~52px rail

- **Brand row:** `ALETH` + `STUDIO` badge + the collapse chevron (`btnToggleLeftSidebar`).
- **Tabs:** Agents · Plans · Files · Env (`#tabSidebarAgents` / `Plans` / `Files` / `Env`,
  wired in `ui/js/wire.js` → `setSidebarTab`). Each section below is one tab panel; the
  Plans tab is also the single entry point for plan selection (§6.3).
- **Workspace card:** shows the active folder (`my_project_workspace`). Clicking it
  opens the native OS folder dialog; the `Change` button does the same. Dialog cleanly
  exits on the first click of `Cancel` or `X` without reopening.
- **MAIN AGENTS / CODER AGENTS:** lists with count pills. Clicking an agent switches
  the stage to the prompt editor view.
- **WORKSPACE FILES (Files tab):** the same listing the Files modal reads, grouped into
  folders in the frontend. Click a folder row to expand/collapse. Each leaf carries a
  colour-coded VCS letter (`.tree-vcs`, amber `M` changed / green `U` new) sourced from
  `tools/git_status.py` — git when the workspace is its own work tree, the
  `.aleth_backups/` snapshots otherwise. The file sizes are gone.
- **ENVIRONMENT (Env tab):** one row per variable in the app's own `.env` — **name, set/unset
  flag and character count only; the value never crosses the bridge**. The eye icon
  toggles a second line with that metadata and the note "value stays in the backend".
  An absent `.env` is not an error; the tab's gear opens Settings.
- **Prompt editor view (full-stage):** back button, tiered editor with the core rules
  rendered read-only, tool pills, character counter, `Reset`, and
  **`Save Custom Directives`** (persists into the `agents/*.py` source and reloads in
  memory with zero app restarts).

Both panes collapse by changing the `<aside>` width (a class + the `--sidebar-*-rail`
tokens), **never** with `display:none` — see bug 8 in §8.

### 2. Centre Pane — the Plan Workbench (and the plan tracker)

`#chatWorkspaceView` → `.stage-center` → `.plan-workbench#emptyStateContainer`. The centre
now does three jobs; **the roadmap — tracker included — lives here**, not in the right rail:

- **Bar:** breadcrumbs (`workspace › PLAN.md` — `#crumbWorkspace` / `#crumbPlanFile`), a
  `● unsaved` marker (`#crumbDirty`), the `[ 🌳 Tree View | 📄 Raw MD ]` toggle
  (`#btnWorkbenchTreeView` / `#btnWorkbenchRawMd`), and `Edit` / `Save` / `Discard`.
  `Save` writes the plan and re-syncs `plan.json`.
- **Tree view** (`#workbenchTreeView`, the default).
- **Bento header** — a fixed-height grid so switching plans cannot make the roadmap below
  it jump: `#bentoSummaryTile` (left, the plan's standing context, expandable into the
  audit rail), `#bentoProgressTile` and `#bentoStatsTile` stacked in the right column. The
  progress tile carries `Roadmap Completion` and `#txtPlanProgressRatio`.
- **Roadmap cards** (`#planTreeContainer`): the three tiers — phase card → step card →
  sub-step row. The progress meter (`#planProgressBarFill` / `…Active` / `…Failed`) reads
  `plan.json`'s metrics. Card details are in §6.3's notes (they moved, they did not change).
- **Raw MD view:** the same document as an editable, gutter-numbered source
  (`#planDoc` / `#planEditorInput`, metadata in `#txtWorkbenchMeta`).
- **Execution stage** (`#executionStage`, hidden at idle): a 50/50 grid — Architect card
  left (blue accents, `#60a5fa`), Coder card right (slate accents, `#cbd5e1`), each with
  live monospaced logs, a status pill, a summary container, and a close button that
  unlocks on completion.
- **Result view** (`#resultView`): a full-stage overlay mounted by the terminal event and
  routed by `state.selectedAction` — the **only** place code-level diffs are shown.
- **Live preview** (`Preview` in the top status bar): a full-stage overlay inside
  `.stage-center`. Reads the workspace's generated interface through the bridge and injects
  it with `srcdoc`. The iframe sandbox is `allow-scripts allow-forms allow-modals
  allow-popups` — deliberately **no** `allow-same-origin`. Because the content is injected
  inline, a page that links **relative** assets will not resolve them.

### 3. Right Pane — the Plan Tracker rail (fixed width, no drag)

`#rightPlanSidebar` is a **fixed-width collapsible rail** — never drag-resizable (the
`#rightSidebarResizeHandle` seam and its listener were retired in Wave 3).

- **Header:** the active plan filename + actions, in order: collapse
  (`#btnToggleRightSidebar`), audit sync (`#btnAuditPlanSync`), create plan
  (`#btnCreatePlanModal`). There is **no** Switch Plan (⇆) button: plan selection has
  exactly one entry point — the **left sidebar's Plans tab** (`#tabSidebarPlans`, whose
  expand button is `#btnExpandSwitchPlan`).
- **Footer:** the console toggle (`#btnConsoleToggle`, a 28px `.rail-console-toggle`,
  Ctrl+`), pinned at the rail's foot in `.plan-sidebar-footer`; it stays visible in both
  the expanded and collapsed states.
- **The roadmap card notes below moved to the centre workbench (§6.2)** when the rail
  became fixed-width. They are recorded here because the *behaviour* is unchanged:
  - Completed: solid green check, title struck through, inline `Rollback` + `Edit`.
  - Failed: `Retry` (`Execute` re-run) + `Fix` + `Edit`; a `✕` ring.
  - Pending: hollow circle, inline `Execute` + `Edit`.
  - Running: amber pulsing indicator; while a run is live the **card of the task the run
    was launched against** hosts an inline **Stop** (it used to hang off an `in_progress`
    status the workflow never writes, so it could never appear).
  - Metadata: the task's **domain pill** (any tag — `[UI]`, `[FE]`, …, from `tag`), plus
    file chips (`📄 auth.py`) under the title.
  - Clicking a card expands its accordion drawer (nested detail bullets, behavioral log,
    sub-steps with a `done/total` bar). Sub-steps are part of the parent card, never tasks:
    no `task-N` id, no metric slot, no effect on `Execute Next Step`.
- The **Global State Summary** renders in the bento summary tile above the roadmap whenever
  the plan carries a `## 🌍 Global State Summary` heading — the parser captures it into
  `plan.json.state_summary`, so it is standing context, not a section.
- Rollback asks for confirmation through the rollback modal; a task with no recorded edits
  reports "This task has recorded no file edits."

### 4. Bottom Dock — 240px when the console is shown, resizable (floor 120px)

Anchored flush below the centre pane — no longer a floating island.
Pane order is load-bearing:

```
#dockResizeHandle  ->  #actionControlPanelContainer  ->  #dockLogs
                         ├── #actionPanelLockOverlay      ├── .console-bar (title + actions)
                         └── #actionDockCard              └── #consoleStream
                             └── #actionDrawerPanel  (the open action's parameter form;
                                                      the dock raises its own floor while
                                                      it is open)
```

- **Action entry point:** the command palette (Ctrl/Cmd+K) or the top bar's `Commands`
  button. The six-button intent strip was a permanent ~90px tax on the dock; it is gone
  (Wave 4) and the palette opens the **command drawer** instead — a pane of the dock, not an
  overlay. It is `display:none` at rest; `.bottom-dock.drawer-open` reveals it.
- **No status row.** Wave 7 retired the read-only `Active Plan:` pill and the dock progress
  pill (the top bar already showed the plan) and moved the completion figure and `Stop Task`
  into the top status bar; the console toggle is at the foot of the right rail. With no drawer
  open the dock head is `display:none`, so at idle the dock is just its top border — unless the
  plan-lock notice is up, which is the one thing that still gives the head height.
- The dock is a CSS **container** (`container-type: inline-size`); the command drawer reflows
  by container width (`@container (max-width: 860px)` drops the subtitle, `620px` stacks the
  body). This is why collapsing a side pane must produce a real width change.

### 5. Modals

Two kinds of overlay live at the `<body>` level — **do not relocate them**:

- **Full-window modals** (`position:absolute` children of `<body>` that cover the window):
  the workspace **file browser** and the **rollback confirmation**.
- **Right-rail slide-in `.side-panel`s** (same element ids, different layout):
  `#auditModalOverlay` (the audit report), the **plan switcher** and **create-plan**
  dialogs. Their open/close handlers only toggle `display`, so preserving the ids kept the
  wiring intact when the centred modals became rail panels.

The action parameter form is **not** here — it is the docked command drawer inside
`#bottomDock` (§6.4), multimodal UI-vision section for `[UI]` tasks and all.

---

## 🛡️ 7. Token Efficiency & Context Protection Rules

1. **Middle Truncation on Shell Output:**
   In `tools/shell_tools.py`, outputs exceeding `MAX_SHELL_OUTPUT_CHARS = 2400` are
   truncated with head/tail preserved:
   ```
   [First 1200 chars] ... [Omitted X characters] ... [Last 1200 chars]
   ```
2. **Middle Truncation on File Reads:**
   In `tools/file_ops.py`, files exceeding `MAX_FILE_READ_CHARS = 6000` are truncated
   similarly — except `.md` plan files **and** `.json` (the plan's machine state).
3. **Environment Values Never Leave Python:**
   `tools/file_ops.py::read_environment_variables()` returns names, a set/unset flag
   and a character count only — never a value. Max `MAX_ENVIRONMENT_VARIABLES = 60`.
4. **Stateless Session Guarantee:**
   No chat history is accumulated across turns. Each prompt initiates a clean session
   anchored exclusively to the plan file and `plan.json`.
5. **Zero-Token Plan Parsing:**
   The plan tree is produced by `tools/plan_parser.py` (markdown-it-py AST + regex),
   not by an LLM call.

---

## 🐛 8. Bugs Resolved (Do Not Reintroduce)

1. **`parse_plan_tree` IndexError:**
   - *Cause:* the plan parser had flawed operator precedence between a `steps` check and
     bullet detection, so an early bullet with an empty `steps` list threw `IndexError`.
     (This logic now lives in `tools/plan_parser.py`; the bug was originally in the
     monolithic `tools/file_tools.py`.)
   - *Fix:* rewritten with explicit grouping, header task detection, and graceful
     fallback step creation.
2. **Missing Plan Silent Fail:**
   - *Cause:* if the plan file didn't exist, the workflow skipped initialization and
     appended directly to an empty file.
   - *Fix:* the workflow checks plan existence and scaffolds a full roadmap first.
3. **Folder Dialog Reopening Loop:**
   - *Cause:* `pywebview.create_file_dialog` returning empty triggered an unconditional
     `tkinter` fallback.
   - *Fix:* explicit cancellation detection returns `{"cancelled": True}` immediately.
4. **Window Renders But Ignores Every Click (frontend):**
   - *Cause:* the frontend was split into per-file `js/*.js` / `styles.css` subresources,
     and pywebview serves the UI from a localhost HTTP server. WebView2 intermittently
     drops an individual fetch; a missing module threw while wiring listeners, which
     silently aborted the rest and left the window rendered but inert (and
     `debug=False` hides the console).
   - *Fix (history):* every module and stylesheet was inlined into `ui/index.html` by
     `tools/build_ui_bundle.py`, so startup performed no subresource fetch, with a head
     watchdog naming any missing entry points and a Node harness pinning the bundles to
     their sources.
   - *Fix (current):* the inlining is gone. `npm run build` emits one JS and one CSS asset
     into `dist/`, and `app.py` maps the host name `aleth.local` onto that directory
     with WebView2's `SetVirtualHostNameToFolderMapping`, so the document and its two
     assets are ordinary https requests the OS layer completes. The watchdog stays (it is
     the only in-window signal when a startup step fails); the Node harness is replaced by
     `tests/ui/structural.spec.mjs`, which asserts the running app's structure in a real
     browser. Do not go back to per-module subresource tags, and do not serve the UI as a
     `file://` document -- that is the failure this entry records.
   - *Do not* re-add `<script src>` / `<link rel="stylesheet">` tags for local files,
     and always re-run the build script after editing `ui/js/` or `ui/css/`.
5. **UI Template Selected For Backend Tasks:**
   - *Cause:* `templates.select` matched its UI keywords as bare substrings, so any
     title merely containing the letters won: `"Build a weather API"` became an HTML
     dashboard because of the `ui` in `build`, and `"Code review"` because of the
     `view` in `review`.
   - *Fix:* keywords are matched on word boundaries (`\b(?:ui|frontend|interface|view)s?\b`),
     so the weather template wins for those titles while plurals such as
     `"Dashboard views"` still match.
6. **Non-Plan Markdown Offered As A Switchable Plan:**
   - *Cause:* `list_plan_files` returned every root `.md`, so a tracker or README was
     listed as a plan; selecting one made the Dual-Sync engine compile a non-plan
     document, surfacing as a blank plan tree with every action locked.
   - *Fix:* `NON_PLAN_MD_FILES` (progress / readme / changelog, case-insensitive) is
     skipped. The files stay visible in the file explorer.
7. **Offline Fallback Disguised A Dead Bridge:**
   - *Cause:* `initFallbackMode` seeded a fake project whenever `window.pywebview` was
     absent, so a failed handshake produced a fully populated window showing a project
     the user does not have.
   - *Fix:* the demo data is opt-in (`?demo=1`) for layout previews only; the default
     path reports a visible "bridge unavailable" failure instead. The 400 ms fallback
     timer is also cancelled when `pywebviewready` arrives first.
8. **Collapsing A Side Pane Froze The Dock's Layout:**
   - *Cause:* `.bottom-dock` is `container-type: inline-size`, and the action labels are
     hidden by a container query. Hiding a pane with `display:none` removes it from the
     layout without changing any width the dock can observe, so the centre content
     reflowed while the dock kept its old label/icon arrangement — and the toolbar
     buttons stayed visually pinned in place.
   - *Fix:* collapse changes the pane's **width** to a rail token
     (`--sidebar-left-rail`, `--sidebar-right-rail`) and flips a class, never
     `display:none`. The dock then reflows correctly.
9. **Indented Checkboxes Became Extra Milestones:**
   - *Cause:* the milestone scan stops at the first `list_item_close`, so a task's first
     nested checkbox was absorbed into `details` (with its literal `[ ] ` prefix) and every
     later one was hardened into a sibling top-level task. A 16-task roadmap parsed as 21
     tasks, and the progress meter counted sub-steps as milestones.
   - *Fix:* `_collect_sub_steps` in `tools/plan_parser.py` runs as a **separate token pass**
     (the milestone scan itself is unchanged) and folds the indented checkboxes into the
     parent's `sub_steps[]`, flattened at any indent depth. The claimed items are skipped
     by index, not by title, so two identically titled tasks cannot collide. The compiler
     re-indents them, and the round trip is byte-stable.
10. **`[UI]` Tag Matched Anywhere In A Title:**
   - *Cause:* `is_ui` was a bare substring test over the whole title, so a task that merely
     *mentioned* the tag (e.g. ``Wire Laya zero-token `[UI]` task tagging into …``) was
     stripped mid-sentence and left an orphaned empty code span, plus a pill it had not
     earned. Same bug class as #5, in the parser instead of the template selector.
   - *Fix:* `_split_ui_tag` recognises `[UI]` only as a standalone token, which is exactly
     the form the compiler writes (`- [ ] [UI] Title`). A backticked or mid-sentence `[UI]`
     is left alone. *Consequence:* a plan whose only `[UI]` mention is inside backticks now
     shows **no** domain pill — that is correct, but it is a visible change.
11. **`## 🌍 Global State Summary` Was Erased On Save:**
   - *Cause:* the heading parsed as a section with zero tasks, its prose bullets were
     dropped from `plan.json`, and the compiler re-emitted nothing — so the first in-app
     save deleted the plan's standing context.
   - *Fix:* `_collect_state_summary` captures the block into `state_summary` and the
     heading is excluded from `sections[]`; `compile_plan_json_to_markdown` re-emits it
     directly under the H1. Any in-app write (Save, executing a task, rollback, plan
     switch) now preserves it.
12. **An Orphan `</div>` Took The Whole Right Pane Off Screen:**
    - *Cause:* moving the action form out of the floating overlay and into the dock was done
      by a script that sliced the old markup up to a `<div class="modal-footer">` marker.
      The slice swallowed the `</div>` that closed `.modal-body`, so the dock ended one
      nesting level early and every later sibling — the plan sidebar among them — was hoisted
      out of the rendered tree. Because it is a parse fault and not a logic fault, it
      presented **even with the drawer closed**, and it looked intermittent only because the
      hoisted node sometimes still landed on screen.
    - *Fix (history):* the extra closer was removed and the markup balanced (`<div>` count ==
      `</div>` count across the region between the two bundle regions, 230 and 230 at the
      time), and that count was re-run after any hand edit to `ui/index.html`.
    - *Fix (current):* the counting gate is gone with the bundle regions, and the defect is
      caught the way it actually presents — by asserting the rendered structure in a browser.
      A hoisted sibling fails `tests/ui/structural.spec.mjs` (the four regions mount, the DOM
      cache resolves, the tree renders) instead of being inferred from arithmetic. The
      lesson stands: after a hand edit to the markup, run `npx playwright test`.
13. **`.drawer-action-btn` Lost To A Bundle-Order Tie:**
    - *Cause:* the bundle concatenates `actions.css` **before** `modals.css`, and
      `.drawer-action-btn` / `.btn-dialog-primary` have equal specificity (0,1,0). The later
      rule won, so the drawer's `height: 24px; padding: 0 12px; font-size: 11.5px` was
      overridden by `flex: 1; padding: 10px 18px; font-size: 13px` and a 13px label spilled
      out of a 24px button.
    - *Fix:* the drawer's button rules are scoped `.action-drawer-panel .drawer-action-btn`
      (0,2,0), which beats the modal rule whatever the order. Do not unscope them, and do not
      reorder the stylesheet list in the build plugin's `CSS_MODULES` to "fix" this.
14. **The Drawer's Commit Row Belonged At The Bottom:**
    - *Cause:* Confirm/Cancel were placed in `.drawer-head`, so the form read as sitting
      underneath the buttons instead of above them.
    - *Fix:* a `.drawer-foot` row closes `#actionDrawerPanel` and only the `×`
      (`#btnCloseParamModal`) stays in the head, as a pane's close affordance. Heights:
      drawer 184px, dock floor 424px — keep the invariant *dock `min-height` = its 240px
      default + the drawer's height*, or the console shrinks when a drawer opens.
15. **The Architect Reported "Verified & Approved" Without Reading Its Own Check:**
    - *Cause:* `custom_action` ran `py_compile`, stored the output in `v_res`, and never
      inspected it — the verdict was a literal string. A custom directive that wrote broken
      code was reported as verified.
    - *Fix:* the verdict comes from `command_failed(v_res)`, mirroring `next_step_action`.
16. **A Failed Deliverable Write Was Narrated As Success:**
    - *Cause:* `tools/file_ops.py::write_file` returns an error **string** on failure rather
      than raising, and the workflow branches ignored the return value — so a failed write
      still emitted "Successfully patched" and `next_step` still marked the task completed.
    - *Fix:* a failed deliverable write must abort the action before a false verdict is recorded.
      The guard used to be one `_write_checked()` at six call sites; Phase 24 deleted that function
      along with the write path it wrapped, because the executor publishes through the MCP
      filesystem server (`client.write_file`). The invariant now rests on that boundary: a refused
      write raises `MCPError`, and `executor._apply_one` turns it into a reported
      `AppliedEdit(success=False)` — never a returned success string.
17. **A Plan "Saved" When It Was Not, And A Corrupt Plan Looked Empty:**
    - *Cause:* `save_plan_state` caught its write/compile failures, printed them, and still
      returned the dict — so every caller emitted `plan_updated` for state the disk never
      received. Separately, an unreadable `plan.json` silently degraded to an empty plan.
    - *Fix:* it raises `PlanWriteError` now, and a load remembers why it failed
      (`last_plan_load_error()` → `load_error` on the payload), which the UI reports.
18. **A Crashed Run Read As A Clean Finish:**
    - *Cause:* the runner emits `workflow_complete{status:"error"}` on any exception, but
      `finalizeWorkflow` treated every non-`stopped` status as success — "Ready" plus a
      "Task concluded successfully!" toast, and it even mounted a result view.
    - *Fix:* `status` now branches to **Run Failed** (red dot, error toast); only `finished`
      mounts the result view; and the client run lock is backed by a **server-side** refusal
      in `start_execution` — a reload mid-run can no longer defeat it, because the UI
      re-arms from `get_run_state()`.
19. **A Name That Resolved Everywhere Except In Five Renderers (ES modules):**
    - *Cause:* the conversion to ES modules generated imports from a scan of the sources,
      and that scan treated any function-scope binding as making the name local to the
      whole file. `result-view.js` declares `const state = task.status === ...` inside one
      renderer, so the shared `state` was never imported — and the other 25 uses of `state`
      in the file became references to a name that no longer existed. The build stayed
      green: an unresolved identifier is a *global* to a bundler, not an error.
    - *Fix:* imports are computed from **module-scope** declarations only. A function-scope
      binding shadows the import inside its own function exactly as it shadowed the global
      before, so it is never a reason to skip the import.
    - *Guard:* `npm run lint` (gate A) now catches this class — `no-undef` was meaningless
      while the modules shared one global scope and is exactly right now that they do not —
      and `tests/ui/interactions.spec.mjs` sweeps **every** action's result view, which is
      how this one was found. Do not "fix" a `no-undef` report by adding a global.
20. **The "Restricted" Architect Shell Ran Arbitrary Code:**
    - *Cause:* the allow-list matched only the *leading* token, and `python` was an allowed
      prefix — so `python -c "import shutil; shutil.rmtree(...)"` (no file needed) and
      `python -m pip install x` (environment mutation) both sailed through, and a `;` after
      an allowed first token executed a second, unchecked command.
    - *Fix:* `python`/`python3`/`py` are no longer plain prefixes. Inline code (`-c`, `-i`,
      stdin `-`) is refused; `-m <module>` is screened against a verification whitelist
      (`pytest`/`py_compile`/`compileall`/`unittest`) and the mutation sub-command list; and
      sequential separators (`;`, `&&`, `||`, backtick, `$(`) are refused.
    - *Guard:* `tests/test_security_boundaries.py::RestrictedShellBoundaryTests`. Residual
      risk — pipes/redirection and `shell=True` itself — needs real OS isolation, not a
      substring screen; see §11.4.
21. **A Zero-Token Plan Parse Paid For The Whole Agent SDK:**
    - *Cause:* `import tools.plan_parser` ran `tools/__init__`, whose eager re-exports pulled
      `tools.file_tools` → `tools.shell_tools` → `langchain_core` (605 ms); and
      `_vocabulary_json()` imported `orchestration.workflow.templates`, whose package
      `__init__` pulls the agent catalogue and its SDK stack (2,152 ms). Cold start: 2.57 s.
    - *Fix:* `tools/__init__` resolves its flat names lazily (PEP 562); `UI_KEYWORD_RE` moved
      to `tools.task_tags` (imports nothing heavier than `re`); `_vocabulary_json()` memoised.
      The cold path is now ≈31.5 ms (import 27.8 ms + first parse 3.7 ms).
    - *Guard:* `tests/test_security_boundaries.py::ColdStartImportTests` pins that the parser
      import loads neither `langchain_core` nor `orchestration`.
22. **The Frontend Was One Strongly-Connected Component:**
    - *Cause:* 17 of 22 `ui/js` modules formed a single SCC (9 mutual imports). Low-level
      renderers imported their own *callers*: `plan-tree.js` imported `actions` /
      `plan-modals` / `sidebar` / `workbench` / `workspace` to call handlers from its
      rendered markup, `agent-events.js` imported `actions` and `bootstrap`, `console.js`
      and `plan-modals.js` imported `actions`, `bootstrap.js` imported `wire`, and almost
      every module imported `showToast` from `visuals.js` — which imports the event handler.
    - *Fix:* a leaf `ui/js/bus.js` (pub/sub, imports nothing) inverts every back-edge --
      renderers publish intents and `ui/js/wire.js`, the single composition layer, maps them
      at module top level; `showToast`/`reportStartupFailure` moved to a leaf `ui/js/notify.js`.
      `ui/js` is now acyclic: 24 modules, 122 edges, 0 cycles.
    - *Guard:* `import/no-cycle` (ESLint) **and** dependency-cruiser `no-circular` both run
      under `npm run lint` and fail on a reintroduced cycle.
23. **Every JSON Boundary Was Read By Convention:**
    - *Cause:* 21 `json.loads` sites trusted the parsed shape. The plan dict, the plan-state
      envelopes, the event builders and the tool-result envelopes had no schema, so a
      renamed, mistyped or invented field rode across silently.
    - *Fix:* `tools/payloads.py` defines strict Pydantic v2 models -- `extra="forbid"` on
      every one (an unknown field is **rejected**; a missing field takes its documented
      default). Wired into `tools/plan_parser.py`, `tools/plan_state.py`,
      `orchestration/workflow/events.py`, `orchestration/workflow/context.py` and the
      `recovery`/`git_status`/`test_runner`/`settings` envelopes.
    - *Guard:* `tests/test_payloads.py` proves each modelled boundary rejects an unknown field.
      Never relax to `extra="allow"`/`"ignore"`; add the real field to the model instead.
24. **Timing Races In The Frontend Wiring:**
    - *Cause:* a 400 ms bridge fallback raced `pywebviewready`, a `0 ms` boot check raced
      first paint, six 60 ms focus delays assumed layout had settled, and the offline
      simulator ran five independent timers.
    - *Fix:* one bridge settle path (event plus one fallback, first wins, cannot double-fire),
      `requestAnimationFrame` for boot verification and `focusAfterLayout()` for focus, and a
      single awaited simulator sequence.
    - *Guard:* the wiring is exercised in a real browser by the Playwright suite (§3 gate C).
25. **Untrusted Commands Ran With Full User Privileges:**
    - *Cause:* `run_command_in_workspace` was a bare `subprocess.run(..., shell=True)`:
      no containment (a child could outlive the run or fork-bomb the host), no privilege
      reduction, and the child inherited the whole environment -- including the `.env`
      API keys.
    - *Fix:* execution is contained in a **Docker container** (`tools/docker_sandbox.py`),
      reached through `tools/mcp_exec_server.py`. Every command runs with `--network=none`,
      `--user <uid>:<gid>` (the host's identity, so file ownership is not mangled), the
      workspace as the only OCI bind mount and the container's working directory, and
      `--memory`/`--cpus`/`--pids-limit` caps, `--cap-drop=ALL` and
      `--security-opt no-new-privileges`. The process tree is owned by the named, `--rm`
      container and is force-removed on timeout. Nothing is passed with `-e`/`--env-file`, so
      the `.env` credentials are unreadable by generated code. When the daemon cannot be
      reached the command is **refused** -- there is no native fallback. (This replaced the
      earlier in-process perimeter, `tools/sandbox.py` -- a Windows Job Object + restricted
      token, POSIX rlimits + a process group. That module is deleted.)
    - *Guard:* `tests/test_docker_sandbox.py` proves the argv contract, the output/exit
      plumbing, the timeout-and-force-remove and the fail-closed refusal; the daemon-backed
      tests run whenever a Docker daemon is present. `tools/test_runner.py` (pytest) goes
      through the same perimeter, because pytest imports and executes the workspace's own code
      -- `tests/test_security_boundaries.py` pins that it has no host `subprocess` path.
26. **Every DOM Sink Was A Potential Script Execution:**
    - *Cause:* 62 `innerHTML` assignments built from template strings, one raw
      `agent.display_name` interpolation (`agents.js`), and a `srcdoc` iframe fed
      workspace-generated HTML (`preview.js`). A missed `escapeHtml()` was code execution
      in the app origin.
    - *Fix:* `ui/js/safe-dom.js` is the single HTML choke point -- an explicit DOMPurify
      allowlist (`setHtml`) plus `setText`. No raw `.innerHTML =` remains outside it; the
      `srcdoc` sink is sanitized and its `sandbox` attribute re-applied without
      `allow-same-origin`/`allow-scripts`.
    - *Guard:* `tests/ui/security.spec.mjs` asserts an injected `<script>`/`onerror`
      payload does not execute.
27. **A `legacy-peer-deps` Bypass Held The Lint Gate Together:**
    - *Cause:* `eslint-plugin-import@2` peers `eslint <=9` while the repo pins eslint 10,
      so the install only worked with `--legacy-peer-deps`.
    - *Fix:* switched to `eslint-plugin-import-x` (maintained fork; peer range includes
      eslint 10) and deleted `.npmrc`. `npm install` / `npm ci` now resolve normally.
28. **A Background Swarm Tick Outlived Its Database:**
    - *Cause:* a worker's completion is delivered by a `Future` callback on the pool's own
      thread, and the callback's trailing `tick()` opens a fresh SQLite connection. A test that
      unlinked its temporary workspace while a callback was still pending produced an unhandled
      `sqlite3.OperationalError` from inside `concurrent.futures` -- nothing above a callback can
      catch a raise, so the traceback was printed and lost, and the same race can corrupt a WAL
      file under concurrent runs.
    - *Fix:* `Swarm.drain(timeout)` waits for dispatched work to settle and
      `Swarm.shutdown(timeout)` drains then closes; `BridgeAPI.shutdown_swarm()` is the app-side
      handle; the characterization harness drains the bridge's swarm *before* unlinking its
      workspace. The callback is now exception-safe -- it reports to stderr rather than raising,
      because a callback that raises is unrecoverable by construction.
    - *Guard:* `tests/test_swarm.py::TestLifecycleAndTeardown` pins drain/shutdown and proves a
      completion whose database has gone does not raise.
29. **A Container Write Through A Windows Drive Destroyed Host Permissions:**
    - *Cause:* with the engine in WSL, a Windows-drive workspace is bind-mounted as `/mnt/c`
      (9p DrvFs). Files a container created there landed with mode `0000` and no Windows ACL:
      `icacls` answered "Access is denied", Python raised `PermissionError`, and the file was
      effectively lost to the user. `--user` was not the cause — root-created files were equally
      unreadable.
    - *Fix:* the perimeter refuses to bridge an OS boundary at all. `docker_bin()` raises
      `EnvironmentError` when the client is not on `PATH`, and the workspace path is used as a
      path *in the daemon's filesystem* — so the orchestrator runs where the daemon runs, on the
      Linux filesystem, with no path translation and no proxy. A proxy would also break the
      process-tree guarantee: killing the proxy leaves the container orphaned.
    - *Guard:* `tests/test_docker_sandbox.py::LegacyPerimeterRemovedTests` pins the absence of
      bridge machinery and the presence of the fatal error, and the daemon-backed tests assert
      the file the container wrote is readable, rewritable and owned by the mapped identity.

---

## 📋 9. Current Status of `PLAN.md`

**The active plan is `PLAN.md` at the repo root** (`PLAN_DIR == PROJECT_ROOT`), titled
**"Aleth Studio Architecture Upgrade Roadmap"** — a *tracked* file, with its
`plan.json` twin beside it (gitignored). It is **not** in `my_project_workspace/`; that
directory is the sandbox for generated code (§2).

As of the last rewrite, **every task on the roadmap is complete — 23/23 (100%)**, across
six sections:

| Section | Tasks | State |
| --- | --- | --- |
| 1. Data Layer, Parser & Context Engine | 4 | all `[x]` |
| 2. Laya System 1 Integration | 7 | all `[x]` |
| 3. Center-Stage & Workbench Redesign | 3 | all `[x]` |
| 4. Connected Dock, Console & Visual Polish | 3 | all `[x]` |
| 5. Compiled Core, Mapped Assets & Modern Gates | 3 | all `[x]` |
| 6. ES Modules & The Store Boundary | 3 | all `[x]` |

Section 5 moved the plan engine into Rust, replaced the inlined frontend bundle with a Vite
build served through a WebView2 virtual host, and swapped the markup-counting gate for
Playwright. Section 6 closed the testing gap first, put the shared state behind
`ui/js/store.js`, and converted `ui/js` to ES modules — which is what made the lint in §3
gate A possible at all. Details in §3.1 (the core), §3 (the gates) and the frontend protocol
below it; the standing decisions are in the plan's own Global State Summary.

> Re-read `PLAN.md` for the authoritative detail; its `## 🌍 Global State Summary` carries
> the settled decisions. This table is a snapshot, not a substitute.

Notes for whoever continues:

- The plan **is** written by the app now, not only by hand: `add_plan_task` (the one-click
  "Add to Plan"), the Update Plan admin bypass, rollback and a plan switch all reach
  `save_plan_state`. An automatic before-image of the last revision is kept in
  `my_project_workspace/.aleth_backups/plan_revision/` — what the result view's
  **Revert Changes** restores.
- `tests/_plan_prebak.md` is the **previous** plan ("FastAPI Cloud Weather Microservice",
  10 tasks). It is untracked and, since `plan.json` was rehydrated, it is the last copy —
  do not delete it without asking.
- `my_project_workspace/` still holds the generated artifacts of the earlier weather plan
  (`main.py`, `test_main.py`, `weather_api.py`, `test_weather_api.py`, `ui_view.html`,
  `test_ui_view.py`, `PROGRESS.md`) and `.aleth_backups/` with keys `bugfix`,
  `custom`, `task-1` … `task-7`. They are unrelated to the active plan.
- The workspace is gitignored, so `list_directory`/`find_path` do not show it — use the
  shell. If a plan edit appears to do nothing, remember `load_plan_state` rehydrates only
  when `md_mtime > json_mtime` (strict); force it with `load_plan_state(force_sync=True)`.
  A corrupt `plan.json` is now reported rather than silently loading as empty (§8 bug 17).

---

## 🎯 10. How Incoming Agents Should Continue

1. **The roadmap is complete (23/23).** `PLAN.md` at the repo root is the record. To start
   something new, add a milestone (Update Plan, or the palette's Custom action) or create a
   fresh plan (§10.4). Task-keyed rollback/diff snapshots use the `task-N` ids (`task-1` …
   `task-23` here); sub-steps have ids like `task-1-sub-2` and own no snapshots.
2. **To pick up the user's backlog:** read `PLAN.md` (repo root) first — it is the roadmap,
   not a description of it. Its `## 🌍 Global State Summary` is the standing context; keep
   those bullets true when a milestone lands.
3. **To inspect or modify agent prompts:**
   - Open the desktop app (`app.py`), click any agent in the left pane's **Agents** tab,
     edit the prompt, and click **`Save Custom Directives`**. The core rules tier is
     read-only.
4. **To switch or create a new project:**
   - Click the workspace card at the top of the left pane to select a new directory.
   - Plan selection is the left sidebar's **Plans** tab; the rail has no ⇄ button (it was
     removed in Wave 7).
   - Click the `+` in the **right rail's header** to scaffold a fresh `.md` plan. A new plan
     is seeded with a Global State Summary by `orchestration/plan_session.py`.
5. **Before claiming any change is done:**
   - Run all the gates in §3, then actually launch the app. A window that renders but
     ignores input is the historical failure mode here, and it is intermittent — launch
     several times before trusting a UI change.
   - A frontend change needs `npm run build` *before* the app is launched: `app.py` serves
     `dist/`, and it refuses to start (with the build command printed) when the build is
     missing.
6. **The architecture as of this handoff:** the plan engine is a compiled Rust extension
   (`crates/deepagents_core`, PyO3 + maturin). `tools/plan_parser.py`, `tools/plan_state.py`,
   `orchestration/workflow/context.py`, `tools/file_ops.read_file`,
   `tools/shell_tools.truncate_output_for_token_efficiency` and `tools/test_runner._truncate`
   are wrappers over it; the tag vocabulary deliberately stays in Python
   (`tools/task_tags.py`) and is passed in. The frontend is ES modules built by Vite into
   `dist/`, served through a WebView2 virtual-host mapping, with its shared state owned by
   `ui/js/store.js`. The gates are green: 444 pytest tests, 39 Rust tests, 30 Playwright
   tests, a clean ESLint run, a **0-cycle** dependency-cruiser gate, and the shared-state
   invariant. Every JSON boundary is validated against strict (`extra="forbid"`) Pydantic
   contracts in `tools/payloads.py`, `ui/js` is a DAG, every DOM sink is sanitized
   (`ui/js/safe-dom.js`), and command execution runs in a container
   (`tools/docker_sandbox.py`) -- see §8 bugs 22–27 and §11.4.
7. **Uncommitted work — current state.** The tree is **not clean**: the entire compiled-core
   / Vite / ES-module change set is uncommitted. `git status --short` shows 41 modified
   tracked files (incl. `app.py`, `tools/plan_parser.py`, `tools/plan_state.py`, 20 of the
   22 `ui/js` modules, `ui/index.html`, `HANDOFF.md`, `PLAN.md`), 3 deletions
   (`tools/build_ui_bundle.py`, `ui/js/state.js`, `tests/ui_startup_contract.js`), and 18
   untracked entries — `crates/`, `tests/ui/`, `tests/test_payloads.py`,
   `tests/test_sandbox.py`, `tests/test_security_boundaries.py`, `tools/payloads.py`,
   `tools/sandbox.py`, `tools/check_ui_state.py`, `ui/js/main.js`, `ui/js/store.js`,
   `ui/js/bus.js`, `ui/js/notify.js`, `ui/js/safe-dom.js`, and the build/config files
   (`package.json`, `package-lock.json`, `vite.config.mjs`, `eslint.config.mjs`,
   `playwright.config.mjs`, `pytest.ini`, `.dependency-cruiser.cjs`). Natural commit
   split: (a) Rust core + wrappers, (b) mapped-asset frontend
   + Playwright/pytest gates, (c) ESM migration + store boundary + lint.
   Earlier history: the three polishing passes (`1ST`–`3RD_POLISHING.md`) and their audit
   follow-ups landed in layered commits — the bento workbench and command palette, the
   elastic console, the polymorphic result views, a real `add_plan_task` and a
   plan-revision revert, a server-authoritative run lock, image-vision input for `[UI]`
   tasks, and the pre-launch audit fixes (Go 15–19). The roadmap's *own* plan file remains
   the source of truth for what is left — re-read it rather than trusting a list here.

---

## 🧭 11. Architectural Handoff Specification (Senior System Review)

Static-analysis artifact. Every claim is derived from the working tree, the configuration
files, or the existing specs; measured figures were produced on this machine on the review
date and are marked as measured. Where a claim is an inference it is labelled. It records
**what is**, not what is planned — it is the document to read before a security, performance
or maintainability review.

### 11.1 System Topology & Tech Stack

There are **no networked services** in the runtime path: one desktop process with four
co-located tiers.

| Tier | Language / runtime | Version | Entry |
| --- | --- | --- | --- |
| A — Frontend | JS ES modules (22) + CSS (19) | Vite 7.3.6 | `dist/index.html` (single module script) |
| B — Python host | Python | 3.12.9 (`venv/`) | `app.py` (GUI), `main.py --cli` |
| C — Compiled core | Rust | edition 2021, host-neutral `stable` (GNU selected per machine) | `crates/deepagents_core` (PyO3 + maturin) |
| D — State on disk | filesystem | — | `PLAN.md` ⇄ `plan.json`, `.env`, backups, `agents/*.py` |

**Communication protocols — none of REST/gRPC/WebSocket.** Python→JS is stringified code
evaluation: `window.evaluate_js(f"window.onAgentEvent && window.onAgentEvent({escaped_json});")`
(`app.py:225`), plus the detached console's second window built from an inline HTML+JS
string (`app.py:51–84, 189–197`). JS→Python is pywebview's `js_api` proxy over `BridgeAPI`
(46 methods). Python↔Rust is in-process PyO3 with `serde_json` text on every crossing
(`crates/deepagents_core/src/lib.rs`). Frontend delivery is a WebView2 virtual-host mapping
(`SetVirtualHostNameToFolderMapping("aleth.local", dist/)`, installed by
monkeypatching `edgechromium.EdgeChrome.on_webview_ready` at class level, `app.py:109–160`);
`file://` is the off-Windows fallback. The single external integration point is an
OpenAI-compatible endpoint (`orchestration/system2.py:119–177`: `timeout=180 s`,
`max_tokens=16384`, optional `image_url` data URLs).

Dependencies: Python ~110 packages (`pywebview 6.2.1`, `pythonnet 3.1.0`, `openai 3.19.2`,
`langchain 1.4.2`/`langgraph 1.2.12`, optional `torch 2.14.0`/`transformers 5.17.0`,
`pytest 9.1.1` + `pytest-xdist 3.8.0`); Node devDeps only (`vite 7.3.6`,
`@playwright/test 1.63.0`, `eslint 10.11.0`; lockfile = 144 packages); Rust 6 direct / 39
transitive (`pyo3`, `pulldown-cmark`, `serde_json`, `serde`, `regex`, `fs2`). No
`requirements.txt` exists.

### 11.2 Data Flow & State Management

No database. Persistence is a dual-representation file pair plus side artifacts, under two
pointers owned by `tools/workspace.py` (`PROJECT_DIR = my_project_workspace/`,
`PLAN_DIR = repo root`).

| Artifact | Format | Tracked | Writer |
| --- | --- | --- | --- |
| `PLAN.md` | Markdown, 4 marks `[x] [-] [ ] [!]` | yes | Rust compiler; humans/agents may edit |
| `plan.json` | JSON indent 2, 1,931 lines / 73,901 B | no | `state.rs` |
| `.env` | dotenv | no | operator; `override=True` (`env_boot.py:50`) |
| `agents/*.py` | Python source | yes | runtime regex rewrite (`prompt_editor.py`) |
| `.aleth_backups/**` + `_meta.json` | files + JSON | no | `tools/recovery.py` (unpruned) |

**State integrity** is enforced by three mechanisms: (1) one `fs2` exclusive lock over both
files, on a temp-dir file named `aleth_plan_locks/<fnv1a64(canonical(plan_dir))>.lock`
(`state.rs:37–92`); (2) atomic per-file writes — `<path>.tmp<pid>` → `sync_all()` →
`fs::rename`, retried 10×15 ms (`state.rs:108–153`); (3) drift detection on read, rehydrating
the JSON from the Markdown only when `md_mtime > json_mtime` strictly (`state.rs:264–340`).
Save is split across the boundary for testability: Python orchestrates
`prepare_plan_state` → `compile_plan_json_to_markdown` → `write_plan_pair`, and a failure
raises `PlanWriteError` rather than reporting a save the disk never received
(`tools/plan_state.py:129–166`).

**Payload sizes.** `evaluate_js` events are 150–600 B typically, but `plan_updated` carries
the whole plan dict plus parsed tree (~50–74 KB) in one JS statement. Rust FFI payloads are
the plan JSON (~5–75 KB) in and out, uncapped. File-read / shell / test-output budgets are
6,000 / 2,400 / 4,000 chars (`tools/file_ops.py:21`, `tools/shell_tools.py:14`,
`tools/test_runner.py:35`); the preview read is capped at 1.5 M chars. **Known recurring
cost:** `orchestration/workflow/context.py` re-`json.dumps(plan)`es the entire plan on every
`state_summary_block` / `task_slice` / `roadmap_brief` / `slice_task_context` call
(`context.py:43,61,72,81`) — once per Coder turn.

### 11.3 Concurrency & Execution Profile

Synchronous and thread-based; **no asyncio, no multiprocessing, no worker queue**.

| Thread | Created | Stop |
| --- | --- | --- |
| pywebview main loop | `app.py:933` | process lifetime |
| pywebview JS-API thread | pywebview internal | process lifetime |
| Workflow runner (daemon) | `app.py:630` | cooperative `stop_event` |
| Laya re-tag pass (daemon) | `app.py:456` | per invocation |
| Detached-console spawn (daemon) | `app.py:825` | short-lived |
| Laya warm-up (daemon) | `agents/laya_model.py:397` | ~0.8 GB load |

Cancellation is cooperative: a fresh `threading.Event` per run (`registry.py:140–146`) polled
by `should_stop()` (`runner.py:78–79`) between streamed lines (`events.py:58`). An in-flight
`system2.complete` is **not** interruptible — it runs to its 180 s timeout and nothing is
joined, because every workflow thread is a daemon. **Run concurrency is hard-capped at 1**
by an in-memory `_execution_thread.is_alive()` guard (`app.py:615–620`); the UI re-arms from
`get_run_state()` (`app.py:635`). Locks in play: `_console_lock`, `Resolver._lock` +
`_RESOLVER_LOCK`, and the Rust file lock — with no documented ordering between them. No
pytest-wide timeout is configured. Snapshots under `.aleth_backups/**` are never pruned
(no retention code found in `tools/recovery.py`).

### 11.4 Failure Modes & Technical Debt

**Single points of failure.** The Rust `.pyd` is a hard module-scope import in
`tools/plan_parser.py`, `tools/plan_state.py`, `orchestration/workflow/context.py`,
`tools/shell_tools.py` and `tools/test_runner.py` — a stale or ABI-mismatched build takes
planning, state, truncation and verification down together, with no pure-Python fallback.
`registry = AgentRegistry()` is an import-time singleton (`registry.py:157`). The file lock
is keyed by a 64-bit FNV hash (a collision would cross-lock unrelated directories) and
assumes real `flock`/`LockFile` semantics. Atomic writes depend on a bounded 150 ms retry
budget, which this OneDrive-synced tree can exceed (the workspace suite was rewritten around
that). `install_asset_host` is Windows-only and silently degrades to `file://` otherwise —
the documented "renders but ignores every click" failure mode.

**Anti-patterns.** (a) *Stringified code evaluation as the primary IPC*, with `json.dumps`
(which does not escape U+2028/U+2029) as the escaping boundary. (b) *A stringly-typed event
contract duplicated on both sides*: one Python definition (`workflow/events.py`) but two
independent JS `switch`es with no shared constant — `agent-events.js` (18 cases) and
`console.js` (17 cases). **Partially hardened this pass:** `events.py`'s three builders now
validate at construction against `tools/payloads.py`, but the raw types the workflow branches
assemble inline (`delegation`, `plan_updated`, `workflow_complete`, …) still cross as
unvalidated dicts and the two JS switches remain independent — a shared, strictly-typed
contract on both ends is still open. (c) *Untyped payload parsing* —
**closed this pass**: `tools/payloads.py` defines strict (`extra="forbid"`) Pydantic models for
the plan dict, the plan-state envelopes, the event builders and every tool-result envelope, and
the 21 `json.loads` boundaries now validate through them, so an unknown or mistyped field stops
at the boundary rather than being read by convention. (d) *~50 broad `except Exception` blocks* (several effectively silent, e.g.
`workflow/reasoning.py:76`; two deliberate `except BaseException` in `laya_model.py:329,349`
that also swallow `KeyboardInterrupt`). (e) *Token screens over a real perimeter* (`shell_tools.py`) — the `python` prefix bypass is
closed (inline code, interpreter-driven installs and sequential separators are refused; `-m`
modules are screened against a verification whitelist), and command execution now runs in a
**Docker container** (`tools/docker_sandbox.py`) with no network and no host path but the
workspace, so `shell=True` is no longer the boundary — the container is. §12 states exactly
what the perimeter enforces. (f) *DOM injection sinks* — **closed this pass**: every `innerHTML` write in `ui/js`
goes through one allowlist sanitizer (`ui/js/safe-dom.js`); the raw `agent.display_name`
interpolation is escaped and the `preview.js` `srcdoc` sink is sanitized with a script-less
sandbox. `tests/ui/security.spec.mjs` proves injected payloads stay inert. (g) *Implicit timing* — **largely
closed this pass**: the 400 ms bridge fallback race is now a single settle path (one event plus
one fallback, first wins), the `0 ms` boot check runs on `requestAnimationFrame`, the six 60 ms
focus delays are `focusAfterLayout()` on `rAF`, and the simulator's five independent timers are
one awaited sequence. The remaining timers are animation (toast fade, delegation highlight,
console attention) and input debounce. (h) *A cyclic module graph* —
**closed this pass**: `ui/js` is acyclic. A leaf `bus.js` (pub/sub, imports nothing) inverts
every back-edge (renderers publish `action:open` / `run:stop` / `sidebar:render-plans` / …;
`wire.js`, the one composition layer, maps them at module top level), `notify.js` is a leaf
owning `showToast`+`reportStartupFailure`, and `import/no-cycle` (ESLint) + dependency-cruiser
`no-circular` fail `npm run lint` on any reintroduced cycle: 24 modules / 122 edges / 0 cycles. (i) *The state-invariant checker has a blind spot*: `tools/check_ui_state.py`'s
regex cannot see the 3 `Set`-typed fields mutated via `.add/.delete/.clear`, so its "239
written fields" undercounts the true 242.

**Hotspots.** Python 9,019 lines / Rust 3,343 / JS 5,936 / CSS 5,859. Worst offenders:
`initEventListeners` (`wire.js:123–382`, 260 lines, 31 listeners, ~48–64 est. branches),
`initDOMElements` (`dom.js:4–264`, 261 lines but flat), `renderPlanTree`
(`plan-tree.js:329–567`, 244 lines, ~7 nesting levels, 10 listeners per render),
`handleAgentEvent` (`agent-events.js:15–202`, 18-case switch), `openActionDrawer`
(`actions.js:14–169`), `renderAnalyzeResult` (`result-view.js:492–578`, ~9 levels),
`orchestration/workflow/actions_impl.py` (1,009 lines), `parser.rs` (1,283 lines).
`app.py` plan reads/writes run synchronously inside bridge methods.

### 11.5 Metrics & Scale Constraints

Measured on this machine, `PLAN.md` = 16,715 B (16.3 KB) → compact plan JSON 50,844 B:

| Path | Target | Measured |
| --- | --- | --- |
| Plan parse, warm (median, n=500 × 2 runs) | **< 1 ms** | **1.72 / 1.78 ms** — target **not met** at current plan size |
| Plan parse, warm p95 / p99 | — | 1.99–2.03 ms / 2.39–2.41 ms |
| Plan parse, **first call (cold)** | — | **3.7 ms** after a 27.8 ms import (~31.5 ms cold path); was **2,571.7 ms** |
| — Rust parse → JSON string | — | 1.06 ms |
| — `json.loads` of the result | — | 0.24 ms |
| — `_vocabulary_json()` warm | — | 0.004 ms |
| Compile JSON → Markdown | — | 0.50 ms |
| AST structure check | — | 0.017 ms |
| Drift resync (`plan.json` ← `PLAN.md`) | < 2 ms | ~1.3 ms warm (parse) + compile; the cold start that used to dominate the first resync is now ≈31 ms |
| Laya gating | sub-50 ms | heuristic µs-class; checkpoint ~1 s/title (hence the offline pass) |

**Cold start — fixed in this pass.** The first parse previously paid 2.57 s, decomposed as
`import tools.plan_parser` = **605 ms** (the `tools` package `__init__` eagerly re-exported
`tools.file_tools` → `tools.shell_tools` → `langchain_core`) and the first
`_vocabulary_json()` = **2,152 ms** (a call-time `from orchestration.workflow.templates import
…` that dragged in the whole `orchestration` package, i.e. the agent catalogue and its SDK
stack). Two changes remove both: `tools/__init__.py` now resolves its flat re-exports lazily
(PEP 562), and `UI_KEYWORD_RE` moved from `orchestration.workflow.templates` into
`tools.task_tags` (which imports nothing heavier than `re`), with `_vocabulary_json()`
memoised. Measured after the change: `import tools.plan_parser` **27.8 ms**, first parse
**3.7 ms**, warm parse 1.786 ms — a cold path of ≈31.5 ms, meeting the "< 50 ms"
requirement. `tests/test_security_boundaries.py::ColdStartImportTests` pins that the parser
import loads neither `langchain_core` nor `orchestration`. (The docs' older "0.985 ms on
11.9 KB" figure remains unreproduced: the steady-state figure is dominated by the Python
wrapper — Rust parse→JSON 1.06 ms + `json.loads` 0.24 ms — at the plan's current 16,715 B.)

Throughput is single-user (one workflow at a time); event rate is throttled to ~50/s by the
20 ms stream pacing. Volume: `PLAN.md` 123 lines / 6 sections / 23 tasks (100%); `plan.json`
1,931 lines; docs `3RD_POLISHING.md` 103,941 B + `HANDOFF.md` 62,068 B; build output
653,755 B (JS 122,577, CSS 81,136, source map 389,982 = 3.18× the shipped JS). The only
unbounded growth vector is `.aleth_backups/**`. No p95/p99 instrumentation exists in
the product code; the figures above are from harness benchmarks, not runtime telemetry.

### 11.6 Verification — measured evidence

Each row is a measurement taken when its phase was verified; the aggregate rows at the top are this
pass (`7590138`, Phase 41.5, before the Phase 42 changes).

| Gate | Command | Result |
| --- | --- | --- |
| Clean tree baseline | `git --no-optional-locks status --short` | clean at `7590138` (Phase 41.5) before this pass |
| Lint | `npm run lint` | clean, exit 0 — ESLint **and** `depcruise`: **0 violations** (35 modules, 188 dependencies) |
| Build | `npm run build` | 59 modules |
| UI tests | `npx playwright test` | **78 passed** in 27.9 s |
| Backend | `venv/Scripts/python.exe -m pytest -m "not llm"` | **1379 passed, 24 skipped, 243 subtests** in 95.25 s (`-n auto`) |
| Backend (Linux, daemon live) | `~/aleth-venv/bin/python -m pytest -m 'not llm' -n4` in WSL | **1372 passed, 5 skipped** on the Phase 39 tree, 0 containers left behind. The WSL venv predates the Phase 41 hard `keyring` import and cannot install it offline, so it is no longer the local authority -- CI is. |
| Shutdown flag & drain | `tests/test_lifecycle.py` (new) | **9 passed** — the flag flips once, the event every loop reads is the same object, drains run newest-first, a failing drain does not stop the others, a drain can unregister itself mid-drain, and `reset` clears flag and registry |
| HTTP 503 | `tests/test_api_gateway.py::ShutdownRefusalTests` | **2 passed** — a request during a shutdown is a 503 naming the reason, and the server registers then forgets its drain |
| Loop boundary | `tests/test_mcp.py::RunLivenessGateTests::test_a_shutdown_stops_the_loop_before_the_next_step` | **passed** — the model is never called after the engine asks the loop to stop; `RunAborted` names the shutdown |
| Signal handler | `tests/test_docker_sandbox.py::ShutdownSweepTests::test_the_signal_handler_drains_before_it_sweeps` | **passed** — the handler flips the flag, drains, then sweeps, and exits 0 |
| WAL contract | `tests/test_state_store.py::StoreTests::test_the_connection_factory_sets_wal_normal_and_a_bounded_wait` | **passed** — `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` on the one connection factory |
| Secrets | `tests/test_harness.py::SecretsTests` + `ZeroTrustAuditTests` | **passed** — a credential round-trips through the keyring, `list` reports names only, an empty value and an unknown provider are refused, no backend is a *reported* failure, the environment is loaded from the store, an exported name wins, and a loaded credential leaves no bytes in the state directory |
| Logging | `tests/test_harness.py::EngineLogTests` | **passed** — one JSON object with timestamp/level/message, the run's correlation id attached, `maxBytes`/`backupCount` at 50 MB × 3, and a second `configure` does not double the lines |
| Pre-flight | `tests/test_harness.py::PreflightTests` + `CliTests` | **passed** — each check answers, each failure carries its fix, a held port names the command to find the holder, `enforce` refuses and says nothing started, and `aleth boot` aborts before reaching the daemon |
| Strict truncation | `tests/test_snapshots.py::DurableMemoryTests` + `SnapshotRepositoryTests` | **passed** — a truncation deletes the ghost row and frees the step for the resumed timeline; a revert drops the abandoned tags and `next_step` returns to `target + 1` |
| Recovery patch | `tests/test_egress.py::EgressApplyTests` | **passed** — a clean egress leaves no patch, a failed one keeps it and names its path, and the artifact passes `git apply --check -p1` |
| Audit | `grep` over the critical path | no debug statements, no `breakpoint()`, no hardcoded absolute paths, no bare `except:`; every broad handler is on a documented observer |
| Durable memory | `tests/test_snapshots.py::DurableMemoryTests` + `tests/test_mcp.py::SnapshotStepTests` | **passed** — a blob round-trips, a no-spend step still records its memory, truncation zeroes the spend but keeps the memory, a pre-Phase-37 table is migrated, and a fresh loop boots holding an earlier pass's step-1 memory |
| Egress | `tests/test_egress.py` | **14 passed** — the baseline is a point in time; the delta is measured against it; a human edit (or deletion) of a path the agent touched is a collision; an unrelated edit is not; the apply lands add/modify/delete, refuses on a collision, and rolls back on a mid-apply failure; the service refuses unverified work, applies verified work, reports a conflict, and `workspace_merge(approve=True)` is the same gate |
| Extraction UI | `tests/ui/live-run.spec.mjs` | **14 passed** — Apply to Project appears only for verified work, posts the egress, hides after applying, and surfaces a collision as a refusal |
| Snapshots | `tests/test_snapshots.py` | **19 passed** — init at step 0 and idempotent; a no-change step is not committed; monotonic step numbering; a revert restores modified/added/deleted files and cleans uncommitted ones; a step with no snapshot resolves to the state as of it; per-step spend accumulates and truncates; the rollback target is paused-only and drains once; `abort_running` marks running intents and leaves settled and paused ones alone |
| Loop rewind | `tests/test_mcp.py::SnapshotStepTests` | **2 passed** — a mutating tool call is committed and tagged, and a rewind queued while the run was held restores the *context window* (step 2's turn is gone from the payload) |
| Rewind UI | `tests/ui/live-run.spec.mjs` | **11 passed** — the picker lists the snapshots newest-first and Revert posts the step; a run with no snapshots disables the control |
| Shutdown | `tests/test_chaos.py::ShutdownHookTests` + `test_snapshots.py::SystemAbortTests` | **passed** — the teardown runs its extra step, a failing extra step still removes containers, no `network create` exists in the production modules, and the network sweep is a check |
| Live rendering | `tests/ui/live-run.spec.mjs` | **9 passed** — the loop's thoughts and tool executions reach the console; the burn counter tracks the budget and warns at the cap; Interrupt posts the held intent; `intent_paused` opens the overlay and resume sends the correction; a refused resume keeps the hold; the window follows the run's stream and returns on the terminal event; a reconnect and a reload both hydrate the hold from the ledger |
| Stream filter | `tests/test_api_gateway.py::IntentStreamFilterTests` + the live filter test | **5 passed** — this intent's frames and untagged lifecycle frames are delivered; another intent's and unparseable frames are dropped |
| Live stream | `tests/test_mcp.py::SteeringAndStreamTests` | **4 passed** — the loop emits `agent_thought`/`token_budget_update`/`tool_execution_start`/`tool_execution_complete` in order and every frame validates against the bus union; an interrupt holds a run and the correction lands as the last `[user]` message; a pause nobody answers raises; and a correction that beats the next gate is still taken |
| Per-intent stream | `tests/test_api_gateway.py` (route + live filter) + `SteeringOperationTests` | **passed** — the route names its intent, `/api/events` stays unfiltered, a filtered stream delivers only its own frames, and both operations are served with their bodies validated |
| Steering operations | `tests/test_characterization.py::SteeringBridgeTests` | **5 passed** — interrupt holds a running intent, resume releases it and queues the correction, and both refuse the wrong state |
| Vendored tokenizer | `tests/test_token_budget.py::VendoredEncodingTests` | **2 passed** — the blob is present with tiktoken's published SHA-256, and the count is tiktoken-exact with no fallback warning |
| Rolling window | `tests/test_mcp.py::ContextWindowTests` | **8 passed** — a summary leads the payload and the window is bounded; without a compressor the exact notice appears; a failing compressor falls back; a short run has neither; the payload stops growing once the window fills; the compressor is handed the previous summary (recursive); and the summary is hard-capped however long the summariser answers (Phase 33) |
| Token bounding | `tests/test_mcp.py::TokenBudgetTests` + `tests/test_token_budget.py` | **passed** — the spend is recorded in the ledger, the breaker stops the loop before the next call and names the count, an already-spent budget refuses immediately, an ungated loop still hits the step ceiling, the columns migrate, and the Node check batches its arguments |
| Runtime selection | `tests/test_project_phases.py` | **17 passed** — the manifests decide the runtime and the image, a polyglot project is refused until it declares one, the syntax fallback fires for Python and Node, and the setup timeout is hard and clamped |
| Gate sealing | `tests/test_staging.py::MergeGateTests` | **7 passed** — a failed verdict refuses, a pass merges, an **unrun** gate refuses, a shadow with no verdict refuses, a rejection is unaffected, and the verdict survives a reload |
| Image contract | `tests/test_docker_sandbox.py::ImageContractTests` | **passed** — an absent runtime image is *pulled*, not built under its tag, and a failed pull is a refusal naming `docker pull` |
| Two phases | `tests/test_project_phases.py` | **11 passed** — detection from manifests, the declaration winning over the table, a malformed declaration falling back, setup on `bridge` and verification on `none` (asserted on the argv), and the tool surface having no network parameter |
| Merge gate | `tests/test_staging.py::MergeGateTests` | **6 passed** — a failed verdict refuses the merge and keeps the shadow, a pass merges, no-suite is mergeable, a rejection is unaffected, and the verdict survives a reload from the manifest |
| State root | `tests/test_state_identity.py::PublishedStateRootTests` | **3 passed** — absolute and published, a child's derivation cannot clobber it, a relative value is refused |
| Network toggle | `tests/test_chaos.py::NetworkIsolationTests` | **passed** — a setup-phase container connects, and a default one cannot |
| Context isolation | `tests/test_run_context.py` | **5 passed** — two concurrent threads hold different ids (barrier-deterministic), a new thread does not inherit its parent's, and an unhandled exception reports `intent=<id>` with the traceback |
| Supervisor faults | `tests/test_swarm.py::TestReactiveTick::test_a_dead_worker_is_recorded_by_the_parent` (+ the negative case) | **passed** — a dead worker writes one `WORKER_TERMINATED` row joined to the tick's intent, and a successful worker writes none |
| Airgap | `tests/test_chaos.py::NetworkBoundaryTests` + `NetworkIsolationTests` | **passed** — no parameter can widen the network (pinned by `inspect.signature`), declaring `net` is reported and does not unseal, and on a live daemon the container cannot connect |
| Correlation | `tests/test_mcp.py::ToolLoopTests::test_the_ceiling_is_recorded_against_the_intent` + `tests/test_telemetry.py::AgentFaultSchemaTests` | **passed** — the row carries `intent_id`, a blank one is refused (`ValueError`), the join narrows to one run, and a pre-Phase-27 ledger is migrated (`intent_id` added, `session_id` dropped) with its old rows intact |
| Liveness gate | `tests/test_mcp.py::RunLivenessGateTests` | **3 passed** — an abort during the model's turn stops the loop before the tool mutates (`written.py` never appears), the fault write is skipped for a dead intent, and an ungated loop still has its ceiling |
| Crash log | `tests/test_run_context.py` | **3 passed** — the id round-trips and clears, an unhandled exception reports `intent=i-crash` with the traceback, and an idle process prints `intent=<none>` |
| Teardown scope | `tests/test_docker_sandbox.py::ShutdownSweepTests` + `tests/test_chaos.py::…leaves_a_neighbours_container_alone` | **passed** — the sweep takes only its own tree; on a live daemon an `owner_pid=1` neighbour's container survives |
| Loop ceiling | `tests/test_mcp.py::ToolLoopTests::test_the_step_ceiling_is_a_fault_not_an_answer` + `…recorded_in_the_telemetry` | **passed** — raises `AgentStepLimitExceeded("Exceeded maximum execution steps")` and writes an `agent_faults` row (kind, role, detail, steps) |
| Tool-result budget | `tests/test_mcp.py::ToolResultBudgetTests` | **4 passed** — a small result is untouched, an oversized one keeps head *and* tail with the marker, and both the file read and the exec block stay ≤ 16 KB |
| Project mutex | `tests/test_intent_ledger.py::ProjectExecutionMutexTests` | **7 passed** — a second intent (and a second *engine* over the same database) cannot claim a project that is executing; the slot frees on settle; a release is not an outcome; a repeated claim cannot re-take it; a blocked intent stays queued at the head; a deferred intent is retried, not failed |
| Engine teardown | `tests/test_docker_sandbox.py::ShutdownSweepTests` + `tests/test_chaos.py::ContainerLifecycleTests::test_the_shutdown_sweep_removes_a_container_whose_owner_is_alive` | **4 + 1 passed** — atexit **and** both signals registered, the registered callable is the sweep, ownership is ignored (detached), and on a live daemon the shutdown sweep removes a container the boot sweep is right to spare |
| Container reaper | `tests/test_chaos.py::ContainerLifecycleTests` in WSL | **8/8 iterations passed** (3 tests each); durations ~3.5–3.8 s, down from a 5.17 s outlier that was hitting the client's grace |
| Removal contract | `tests/test_docker_sandbox.py::ContainerRemovalContractTests` | **3 passed** — detached ⇒ `start_new_session` and no pipes; plain ⇒ captured and awaited; the purge passes the mode through |
| Identity atomicity | `tests/test_state_identity.py::MintedIdentityTests::test_a_torn_mint_cannot_become_a_different_project` | **passed** — a refused rename leaves no partial token and no scratch file |
| I/O roots | `tests/test_staging.py::ExecutionRootBifurcationTests` | **7 passed** — the writer lands in the shadow and not the host, a read sees the agent's own writes across steps, the UI reads still see the live project, an injected root is the only root, an out-of-root path is a `ValueError`, `execution_io`'s code never names `get_project_dir` (AST-pinned), and the run context carries the shadow root |
| Durability + bounding | `tests/test_retention.py` (new) + `tests/test_staging.py` | **48 passed** — ledger retention per table, a ledger-below-its-bound no-op, a never-created ledger skipped, `ids_with_status`, and the boot sweep end-to-end (a crashed intent failed *and* its shadow collected, a completed one kept, a never-ran project's strays collected, the report line, and a failing sweep reported rather than raised); plus the sibling-temporary assertion, a failed merge leaving the host file whole with no litter, and orphaned/manifest-less shadow collection |
| Atomic writes | `tests/test_mcp.py::AtomicFilesystemWriteTests` | **4 passed** — no scratch beside a write, a refused `os.replace` leaving the original whole and no litter, the temporary sharing the target's directory, and `create_file` still create-never-replace |
| Merge boundary | `tests/test_api_gateway.py::WorkspaceBoundaryTests` | **4 passed** — a missing `intent_id` is a 400, a held lock is a **409**, and an accepted merge reaches the service |
| Rust core | `cargo test` (in crate) | not re-run this pass |
| Isolation | `tests/test_docker_sandbox.py` | daemon-backed tests skip on Windows (no docker client); the Linux CI runner is the authority |
| State invariant | `tools/check_ui_state.py` | not re-run this pass |

> Note: the cycle gate uses `eslint-plugin-import-x` (the maintained fork), whose peer range
> includes eslint 10 — so `npm install`/`npm ci` resolve with no `legacy-peer-deps` bypass.
>
> Note: `cargo test` must run **inside** `crates/deepagents_core` (or with the directory as
> the working dir) so the crate's `rust-toolchain.toml` is honoured. Passing `--manifest-path`
> from the repo root ignores that file and builds with whatever the machine's rustup default is.

---

## 🔒 12. Execution Isolation (Docker-Only — and Its Honest Limits)

`tools/docker_sandbox.py` is the execution perimeter. Every command the app did not author
(Coder-generated code, workspace scripts, verification runs) is launched by
`tools/mcp_exec_server.py` through `run_isolated`, which runs it in a **container** rather than
on the host. The old in-process perimeter (`tools/sandbox.py`: a Windows Job Object + restricted
token, POSIX rlimits + a process group) is deleted.

**Enforced.** `docker run` with:

* `--network=none` — no egress and no ingress;
* `--user <uid>:<gid>` — the container process runs as the **host** user, so files written
  through the workspace bind mount keep the caller's ownership instead of becoming root-owned.
  Windows has no POSIX uid, so the value defaults to `1000` and is overridable with
  `ALETH_SANDBOX_UID`/`ALETH_SANDBOX_GID`;
* the workspace as the **only** host path, an OCI bind mount
  (`--mount type=bind,source=<root>,target=/workspace`) that is also the container's working
  directory — so the command's cwd is the workspace root and nothing outside it is reachable;
* `--memory`/`--memory-swap`, `--cpus` and `--pids-limit` caps, so a fork bomb or an OOM cannot
  take the host down. The default budget is **512 MB / 1.0 CPU / 64 pids**, and it is bounded by
  construction: `build_command` clamps to `MAX_MEMORY_MB`/`MAX_CPUS`, so no caller can exceed the
  ceiling. The only widening is the plan-declared `heavy` capability (4096 MB / 2.0 CPU), which
  reaches the exec server's own argv as `--resource-profile` -- never a tool argument. A container
  the kernel's OOM killer ends returns 137, which `run_isolated` marks `oom_killed` and the exec
  server records as `OOM_KILLED` in `execution_telemetry`;
* `--cap-drop=ALL` and `--security-opt no-new-privileges`;
* a named, `--rm` container, so the process tree is owned by the container and dies with it. On
  timeout the container is force-removed by name, because killing the `docker` client alone
  would leave it running.

Nothing is passed with `-e`/`--env-file`, so the child environment is the container's own and
the `.env` credentials are unreadable by generated code — stronger than the old perimeter,
which had to rebuild the environment to strip secret-looking names.

**Refused, never degraded.** If the Docker daemon cannot be reached, or the `docker` client is
missing, `run_isolated` raises `SandboxError` and the exec server reports
`Error: Command refused: container isolation is required but unavailable ...`. There is no
unsandboxed fallback and no `ALETH_SANDBOX=auto`/`=off`: a silent host run is the exact
failure the perimeter exists to prevent. `tests/test_security_boundaries.py`
(`NoImportTimeToolCatalogTests`) pins that no production module shells out
(`subprocess(..., shell=True)`) and that the deleted perimeter cannot return.

**The image contract.** Payloads run in `aleth-sandbox:latest`, built from
`docker/sandbox.Dockerfile` (Python 3.12 plus pytest, pytest-xdist, pytest-timeout, pytest-mock,
coverage, requests, responses and freezegun) because the commands the app runs are a workspace's
own build and test commands. `tools/test_runner.py` — the result view's verdict runner — runs
pytest **in the same container**, since pytest imports and executes the workspace's own
`conftest.py` and test modules. When the image is absent it is built once, behind a
cross-process lock, on first use; a *custom* `ALETH_SANDBOX_IMAGE` that is absent is an
error rather than a build, because the repo's Dockerfile describes the sandbox image and nothing
else. A machine with no reachable runtime gets the `unavailable` verdict, never a host run.

**Run where the daemon runs.** The perimeter speaks to the daemon over the `docker` client and
treats the workspace path as a path *in the daemon's own filesystem*. Both are only true in one
namespace, one filesystem and one signal space, so:

* **The client must be on `PATH`.** If it is not, `docker_bin()` raises `EnvironmentError`
  (surfaced as a `SandboxError` refusal at the tool boundary). A missing client is a fatal
  configuration error, not something to work around — this module is not a path-translation
  utility and does not proxy commands through another OS.
* **No OS bridging.** There is no WSL bridge, no `wslpath` call and no `\\wsl$` rewriting.
  Proxying `docker run` through another OS's interpreter would also break the process-tree
  guarantee: killing the *proxy* leaves the container running in the daemon's namespace,
  orphaned and unreachable by the timeout path.
* **A container is accountable, and a killed server takes it with it.** Every container carries
  `aleth.managed=true`, `aleth.execution_id=<uuid>` and `aleth.owner_pid=<pid>`. The exec server
  tracks its in-flight containers and removes them on `SIGTERM`/`SIGINT`
  (`tools.mcp_exec_server.install_container_reaper`) — the MCP client signals SIGTERM before its
  SIGKILL grace period, so the handler is the one chance to clean up, and without it a container
  whose server was killed outlives everything that knew its name.
  `tools.docker_sandbox.purge_orphaned_containers()` sweeps what is left: with a session id it
  removes that owner's containers, and with none it removes any whose `owner_pid` no longer
  exists in `/proc`. A dead owner *is* the definition of an orphan — that process is the only one
  that would ever have removed it — and where `/proc` cannot answer, nothing is removed rather
  than something that may still be running.
* **A Windows-drive workspace is not usable.** A container writing through a `/mnt/c` (9p DrvFs)
  bind mount creates files with **no usable Windows ACL** — mode `0000`, which Windows can
  neither read nor enumerate. So when the daemon lives on Linux, the orchestrator and its
  workspace live there too.

**Verification.** The daemon-backed tests in `tests/test_docker_sandbox.py` run against the live
engine in the daemon's own environment — no skips — and prove the mount, that the container is
not root, that the network is denied, that a file the container writes is readable *and*
rewritable by the host, and that the file is owned by the identity `--user` mapped (`os.stat` on
the host). The argv contract, the timeout/force-remove and the fail-closed refusal are
additionally proven without a daemon by the in-repo runtime double (`tests/fake_docker.py`).