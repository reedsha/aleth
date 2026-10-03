"""Phase 40: the ``aleth`` entry point -- the only supported way to start the engine.

Two jobs, and both exist because the alternative asks a user to debug their own setup:

``aleth keys``
    Store, list and clear credentials in the **OS keyring**. A value is read from a prompt and
    written to the platform's own store; it is never echoed, never logged, and never written to a
    file. ``list`` reports *names* only, so it cannot leak a secret into a terminal or a transcript.

``aleth boot``
    Run the pre-flight checklist (container runtime, git, the API port, the credential store) and
    **abort with a fix-it report** if anything is wrong. No partial execution: an engine that starts
    with a broken perimeter discovers it after it has accepted a run and started writing state.
    Then open the desktop window.

``aleth serve``
    The same engine, headless (Phase 47): one process, one port. The gateway serves the built
    frontend at ``/`` and the typed API at ``/api`` from a single socket, so there is no second
    server and no Node.js at runtime. This is what the container image runs.

Installed as a console script by ``pyproject.toml`` (``[project.scripts] aleth = "cli:main"``).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import List, Optional

from tools import engine_log, preflight, process_lock, secrets


def _keys_set(args: argparse.Namespace) -> int:
    """Read a credential from a prompt and store it in the keyring."""
    provider = str(args.provider or "").strip().lower()
    if provider not in secrets.PROVIDERS:
        print(
            f"unknown provider {provider!r}; expected one of "
            f"{', '.join(sorted(secrets.PROVIDERS))}",
            file=sys.stderr,
        )
        return 2
    value = getpass.getpass(f"{provider} credential (input hidden): ").strip()
    try:
        secrets.set_secret(provider, value)
    except secrets.SecretError as error:
        print(f"could not store the credential: {error}", file=sys.stderr)
        return 1
    print(f"stored {provider} in the OS keyring (nothing was written to disk).")
    return 0


def _keys_list(_args: argparse.Namespace) -> int:
    """Report which providers hold a credential. Names only -- a value never leaves the store."""
    if not secrets.available():
        print(
            "no OS keyring backend is available; install `keyring` and a platform backend, "
            "or export the credential in the environment.",
            file=sys.stderr,
        )
        return 1
    stored = secrets.stored_providers()
    if not stored:
        print("no credentials are stored.")
        return 0
    for name in stored:
        print(f"{name}: stored (as {secrets.provider_env(name)})")
    return 0


def _keys_clear(args: argparse.Namespace) -> int:
    """Forget one provider's credential, or every one when none is named."""
    wanted = str(args.provider or "").strip().lower()
    targets = [wanted] if wanted else secrets.stored_providers()
    if wanted and wanted not in secrets.PROVIDERS:
        print(f"unknown provider {wanted!r}", file=sys.stderr)
        return 2
    if not targets:
        print("nothing to clear.")
        return 0
    cleared = [name for name in targets if secrets.clear_secret(name)]
    print(f"cleared {len(cleared)} credential(s): {', '.join(cleared) or 'none'}")
    return 0


def _prepare_daemon() -> bool:
    """Everything the daemon must do **before** it serves: teardown, migration, sweeper.

    Shared by ``aleth boot`` and ``aleth serve`` so the two cannot drift. Both migrate before
    anything opens the store (Phase 45) and both start the sweeper that keeps the state bounded
    (Phase 46). Returns ``False`` when the schema cannot be brought up, so the caller refuses to
    start rather than serve a database whose shape it does not understand.
    """
    from storage.intents import abort_running_intents
    from tools.docker_sandbox import install_shutdown_sweep

    # Phase 42: arm the teardown before anything can exit -- a kill must sweep the containers and
    # mark the running intents aborted.
    install_shutdown_sweep(
        "shutdown",
        extra=lambda: abort_running_intents(
            "the engine was shut down before this run finished"
        ),
    )
    from storage.migrations import MigrationError, migrate_on_boot

    try:
        migrate_on_boot()
    except MigrationError:
        return False
    # Phase 46: the sweeper that prunes expired state and reclaims the file. It stops through the
    # same drain the signal handler runs, so a shutdown does not leave it mid-sweep.
    from storage.maintenance import start_maintenance, stop_maintenance
    from tools import lifecycle

    lifecycle.register_drain(stop_maintenance)
    start_maintenance()
    return True


def _boot(args: argparse.Namespace) -> int:
    """Pre-flight, take the process lock, then start the daemon. Aborts on any failed check."""
    from api.server import default_port
    from tools.workspace import state_dir

    port = int(args.port) if args.port else default_port()
    if not preflight.enforce(port=port):
        return 1
    engine_log.configure()
    # Phase 41: one engine per project, decided by an OS file lock rather than a port probe. A
    # stale lock from a hard crash is reaped here, and the Phase 36 sweep runs at the moment the
    # crash is discovered rather than at the next boot.
    from tools.docker_sandbox import purge_managed_containers, sweep_orphaned_containers

    def reap() -> None:
        sweep_orphaned_containers("stale-lock")
        purge_managed_containers()

    with process_lock.process_lock(state_dir(), reap=reap) as state:
        if not state.acquired:
            print(f"\nrefusing to start: {state.detail}", file=sys.stderr)
            print(
                f"  stop it, or remove {state.path} if you are certain it is stale.",
                file=sys.stderr,
            )
            return 1
        if state.stale:
            print(f"[Lock] {state.detail}")
        resolved = secrets.active().providers()
        if resolved:
            print(f"credentials available: {', '.join(resolved)}")
        if not _prepare_daemon():
            return 1
        import app

        app.main()
    return 0


def _serve(args: argparse.Namespace) -> int:
    """The headless daemon: one process, one port, no window (Phase 47).

    The same engine as ``aleth boot`` minus the desktop window, and the reason the container image
    needs no Node.js: the gateway serves the built frontend at ``/`` and the typed API at ``/api``
    from a single socket, so the page's origin *is* the API's origin.
    """
    from api.server import default_port
    from tools.workspace import state_dir

    port = int(args.port) if args.port else default_port()
    if not preflight.enforce(port=port):
        return 1
    engine_log.configure()
    from tools.docker_sandbox import purge_managed_containers, sweep_orphaned_containers

    def reap() -> None:
        sweep_orphaned_containers("stale-lock")
        purge_managed_containers()

    with process_lock.process_lock(state_dir(), reap=reap) as state:
        if not state.acquired:
            print(f"\nrefusing to start: {state.detail}", file=sys.stderr)
            print(
                f"  stop it, or remove {state.path} if you are certain it is stale.",
                file=sys.stderr,
            )
            return 1
        if state.stale:
            print(f"[Lock] {state.detail}")
        resolved = secrets.active().providers()
        if resolved:
            print(f"credentials available: {', '.join(resolved)}")
        if not _prepare_daemon():
            return 1
        import app
        from tools import lifecycle

        index = os.path.join(app.frontend_root(), "index.html")
        if not os.path.isfile(index):
            print("the frontend has not been built: dist/index.html is missing.", file=sys.stderr)
            print(
                "build it with `npm install && npm run build`; the container image already has it.",
                file=sys.stderr,
            )
            return 1
        service = app.EngineService()
        started = service.start_api()
        if not started.get("success"):
            print(
                f"the gateway could not start: {started.get('error', 'unknown')}",
                file=sys.stderr,
            )
            return 1
        lifecycle.register_drain(service.stop_api)
        print(
            f"[aleth] serving {started['base_url']} (loopback only; the UI is at /)",
            flush=True,
        )
        # Block until a signal. The handler flips the flag, drains -- which stops the gateway and the
        # sweeper -- and exits; returning here keeps a normal shutdown observable too.
        lifecycle.shutdown_event().wait()
        service.stop_api()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface: two command groups, no hidden flags."""
    parser = argparse.ArgumentParser(prog="aleth", description="Aleth: the local AI execution engine.")
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("boot", help="run the pre-flight checks and start the engine")
    boot.add_argument("--port", type=int, default=0, help="the API port (default: the configured one)")
    boot.set_defaults(handler=_boot)

    serve = sub.add_parser(
        "serve", help="run the headless server (one process, one port; no desktop window)"
    )
    serve.add_argument("--port", type=int, default=0, help="the API port (default: the configured one)")
    serve.set_defaults(handler=_serve)

    keys = sub.add_parser("keys", help="manage credentials in the OS keyring")
    key_commands = keys.add_subparsers(dest="keys_command", required=True)

    setter = key_commands.add_parser("set", help="store a provider's credential")
    setter.add_argument("provider")
    setter.set_defaults(handler=_keys_set)

    lister = key_commands.add_parser("list", help="show which providers have a credential")
    lister.set_defaults(handler=_keys_list)

    clearer = key_commands.add_parser("clear", help="forget a credential (or all of them)")
    clearer.add_argument("provider", nargs="?", default="")
    clearer.set_defaults(handler=_keys_clear)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """The console-script entry point. Returns the process status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
