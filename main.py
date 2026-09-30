import atexit
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
from tools.docker_sandbox import sweep_orphaned_containers

sweep_orphaned_containers("boot")

# And again on the way out. ``atexit`` covers every graceful exit, including a ``sys.exit`` from
# the bootloader below; a SIGKILL is precisely the case the boot sweep above exists for.
atexit.register(sweep_orphaned_containers, "shutdown")

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
