"""The API gateway: the frontend's only door to engine state.

The desktop UI used to reach the engine through the pywebview JS bridge, which hands the page
*every* backend method -- including the ones that execute. That is a two-way channel into a
process that owns a container runtime, and it makes the frontend's reach impossible to state.

This package replaces it with one unidirectional, typed door:

* **read-only state** (``GET /api/...``) -- the plan DAG, task statuses and the execution
  ledger, projected from SQLite by the engine. The frontend never opens the database and never
  reads a file;
* **intent submission** (``POST /api/intent/execute``) -- the one mutation. It is *validated,
  queued, and executed by the orchestrator*, so a browser request can never spawn a container
  directly, and a run that is already live refuses a second one exactly as a button press would;
* **state streaming** (``GET /api/events``) -- server-sent events, broadcast from the same typed
  bus the desktop window listens on, so the UI is pushed to rather than polling SQLite.

The socket binds ``127.0.0.1`` and refuses anything that is not loopback, with a ``Host``
allow-list (DNS rebinding) and an ``Origin`` allow-list (cross-site requests); see ``api.server``.

Layout, by concern:

    schemas.py   the wire contract: a strict model per request and response
    reads.py     read-only projections of the store and the ledger
    intents.py   the validated intent queue and the loop that drains it
    events.py    the subscriber hub the SSE endpoint streams from
    gateway.py   routing: request -> response, with no socket in sight
    server.py    the loopback HTTP server, the security guard, and the SSE transport
"""
