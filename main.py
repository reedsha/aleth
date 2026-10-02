import sys

from env_boot import load_environment

# ``.env`` wins over the process environment; see env_boot.load_environment.
for _shadowed in load_environment():
    print(f"[Config] .env overrides the exported {_shadowed}")

# The container sweep runs **here** -- before the bootloader probes a tier, before the store is
# opened on import, and before any MCP server is spawned. A hard crash (OOM-kill, power loss)
# bypasses the exec server's SIGTERM handler, so those containers are still running, still
# burning CPU and still holding their file locks; the next boot is the only thing that will ever
# collect them. It reports what it found, because a silent collector hides a systemic crash.
# Unhandled exceptions get the run's correlation id (Phase 27). Installed **first**, before the
# sweeps below: an exception raised by the boot itself is exactly the kind that used to arrive as a
# naked traceback with nothing saying which run it belonged to.
from tools.run_context import install_exception_logging

install_exception_logging()

# The tokenizer's encoding is vendored, and pointing tiktoken at it here is what makes the intent
# budget **exact** rather than estimated -- and offline, because tiktoken otherwise downloads its
# BPE file on first use (Phase 33). ``tools.token_budget`` resolves the same directory itself, so a
# swarm child is covered too; this is the boot's own statement of it.
from tools import token_budget

token_budget.install_cache_dir()

from tools.docker_sandbox import install_shutdown_sweep, sweep_orphaned_containers

sweep_orphaned_containers("boot")

# The state sweep runs immediately after, and still before the bootloader, for the same reason: a
# process that died without unwinding leaves a shadow workspace no run can reach any more and an
# append-only ledger with no end (Phase 22). ``storage.retention`` fails the unfinished intents,
# trims the ledgers to their bound and removes the dead shadows -- and says how many of each, so a
# systemic crash is visible at the next boot rather than absorbed silently.
from storage.retention import sweep_state

sweep_state()

# And on every way out (Phase 25): ``atexit`` for a normal return or a ``sys.exit`` from the
# bootloader below, and SIGINT/SIGTERM for a kill from a terminal, a supervisor or a service
# manager. This one removes **every** managed container rather than only the ones whose owner has
# died -- at shutdown the owner is this process -- so the engine never leaves a container behind
# for the next boot to find. A SIGKILL is precisely the case the boot sweep above exists for.
install_shutdown_sweep("shutdown")

# The bootloader runs here, before anything imports the registry or ``app`` -- both of which open
# the SQLite store on import. The fleet is loaded, validated and *proven* first: a system that
# boots with an endpoint it cannot reach fails later, mid-run, with half a plan on disk.
from core.config import boot_or_exit

boot_or_exit()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from registry import registry
        prompt = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Build a simple weather API in FastAPI."
        print(f"Running in CLI mode: {prompt}")
        registry.run_agent_workflow(prompt, lambda event: print(f"[{event.get('agent', 'System')}] {event.get('type')}: {event.get('text', event.get('message', ''))}"))
    else:
        # Default: Launch LIX PyWebView Desktop UI
        import app
        app.main()
