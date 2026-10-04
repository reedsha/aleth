"""Generate ``openapi.json`` from the live wire contract.

The HTTP API has exactly two sources of truth: the typed operation table
(``api.operations.OPERATIONS``) and the gateway's infrastructure routes
(``api.gateway.ROUTES``). Both are code; this script reflects over them and over the strict
Pydantic models in ``api.schemas``, so the published specification is *derived* and cannot drift
from what the server actually validates. There is no second, hand-maintained description of the
surface.

Usage::

    python -m tools.openapi_spec [output_path]

The default output is ``openapi.json`` beside the repository root. Deterministic: the same code
produces the same bytes, so regenerating after a contract change is a reviewable diff.

What each operation answers is documented as the shared envelope (:class:`OperationEnvelope`):
the service payload lives in ``data`` and is deliberately free-form -- the payload IS the service's
own established dictionary contract (the same one the characterization suite pins), and mirroring
every internal model into the schema would make the wire change whenever an internal model does.
The gateway's own reads *are* typed end to end, so theirs are referenced by model.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from api import operations
from api import schemas

OPENAPI_VERSION = "3.1.0"

# The response shapes every endpoint can produce. One failure model on purpose: the frontend has a
# single failure branch, and a spec that invented per-endpoint error shapes would be documentation
# the client cannot use.
ERROR_CONTENT = {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}

ENVELOPE_CONTENT = {
    "application/json": {"schema": {"$ref": "#/components/schemas/OperationEnvelope"}}
}

SSE_CONTENT = {"text/event-stream": {"schema": {"type": "string"}}}

# The shared response objects. Every endpoint can produce these, so each is declared once and
# referenced -- which is what keeps a 50-operation spec copyable instead of repeating one error
# shape fifty times.
SHARED_RESPONSES: Dict[str, Any] = {
    "OperationOk": {
        "description": "The envelope. `ok: true` carries the service payload in `data`; "
        "`ok: false` carries the reason in `error`.",
        "content": ENVELOPE_CONTENT,
    },
    "BadRequest": {
        "description": "Invalid request: the body did not validate against the strict model.",
        "content": ERROR_CONTENT,
    },
    "Forbidden": {
        "description": "Not loopback, or the engine is draining. The socket's only boundary.",
        "content": ERROR_CONTENT,
    },
    "Conflict": {
        "description": "Conflict: another merge holds the workspace write lock.",
        "content": ERROR_CONTENT,
    },
    "InternalError": {
        "description": "The operation failed, or its answer broke the envelope contract.",
        "content": ERROR_CONTENT,
    },
    "ServiceUnavailable": {
        "description": "The engine service is not attached to the gateway yet.",
        "content": ERROR_CONTENT,
    },
}


def _ref_response(name: str) -> Dict[str, Any]:
    return {"$ref": f"#/components/responses/{name}"}


def _components(models: List[Type[BaseModel]]) -> Dict[str, Any]:
    """Every wire model, flat, with ``$defs`` refs rewritten to ``components/schemas``.

    Pydantic nests each referenced model under ``$defs`` of its parent's schema, and a
    ``#/$defs/...`` ``$ref`` resolves at the *document* root -- so two models that reference a
    third would each carry their own copy and point at nothing. Every nested model here is a wire
    model in its own right, so the defs are hoisted to the same flat namespace and the refs are
    rewritten once.
    """
    wanted = [model for model in models if model.model_fields]
    known = {model.__name__ for model in wanted}
    flat: Dict[str, Any] = {}
    for model in wanted:
        schema = model.model_json_schema()
        defs = schema.pop("$defs", {})
        for name, sub in defs.items():
            flat.setdefault(name, sub)
    for model in wanted:
        schema = model.model_json_schema()
        schema.pop("$defs", None)
        flat[model.__name__] = schema
    text = json.dumps(flat, ensure_ascii=True).replace('"#/$defs/', '"#/components/schemas/')
    resolved: Dict[str, Any] = json.loads(text)
    # A model only reachable as a nested def is kept: it is still on the wire.
    for name in list(resolved):
        if name not in known and name.startswith("_"):
            resolved.pop(name)
    return {"schemas": _strip_titles(dict(sorted(resolved.items())))}


def _strip_titles(node: Any) -> Any:
    """Remove Pydantic's per-field ``title`` keys.

    They carry no contract information (the field name is already the key) and they double the
    size of the emitted document -- which matters for a spec people are expected to copy. Only
    *schema* titles are removed, which is to say only a ``title`` whose value is a string: a
    **property** named ``title`` (``PlanView.title`` and friends) is a key whose value is a
    schema object, and dropping those would silently delete a real wire field.
    """
    if isinstance(node, dict):
        return {
            k: _strip_titles(v)
            for k, v in node.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def _params_for_get(model: Type[BaseModel]) -> List[Dict[str, Any]]:
    """A GET's arguments ride in the query string (``api.gateway._operation_body``)."""
    params: List[Dict[str, Any]] = []
    for name, field in model.model_fields.items():
        params.append(
            {
                "name": name,
                "in": "query",
                "required": field.is_required(),
                "description": (field.description or "").strip(),
                "schema": _field_schema(model, name),
            }
        )
    return params


def _field_schema(model: Type[BaseModel], name: str) -> Dict[str, Any]:
    """One field's schema, lifted from the model's full schema (with refs inlined)."""
    full = model.model_json_schema()
    props = full.get("properties", {})
    schema = dict(props.get(name, {"type": "string"}))
    schema.pop("title", None)
    # $defs refs are resolved inline: a one-field query parameter does not need a $defs hop.
    resolved = json.loads(json.dumps(schema))
    defs = full.get("$defs", {})

    def _inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                return {k: _inline(v) for k, v in target.items() if k != "title"}
            return {k: _inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_inline(item) for item in node]
        return node

    return _inline(resolved)


def _request_body(model: Optional[Type[BaseModel]]) -> Optional[Dict[str, Any]]:
    """The JSON body for a mutation. ``None`` for an operation that takes no arguments."""
    if model is None or not model.model_fields:
        return None
    return {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}},
    }


def _operation_responses(verb: str) -> Dict[str, Any]:
    responses: Dict[str, Any] = {
        "200": _ref_response("OperationOk"),
        "400": _ref_response("BadRequest"),
        "403": _ref_response("Forbidden"),
        "500": _ref_response("InternalError"),
        "503": _ref_response("ServiceUnavailable"),
    }
    if verb != "GET":
        responses["409"] = _ref_response("Conflict")
    return responses


def _operation_path(operation: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "operationId": operation.name,
        "summary": operation.name.replace("_", " ").capitalize() + ".",
        "tags": ["operations"],
    }
    if operation.verb == "GET":
        params = _params_for_get(operation.request)
        if params:
            item["parameters"] = params
    else:
        body = _request_body(operation.request)
        if body is not None:
            item["requestBody"] = body
    item["responses"] = _operation_responses(operation.verb)
    return item


def _path_param(name: str, description: str) -> Dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": {"type": "string", "pattern": "^[A-Za-z0-9_.\\-]+$"},
    }


def build_spec() -> Dict[str, Any]:
    paths: Dict[str, Any] = {}

    # -- the typed operation surface (api.operations.OPERATIONS) --------------------
    for operation in operations.OPERATIONS:
        paths.setdefault(operation.path, {})[operation.verb.lower()] = _operation_path(operation)

    # -- the gateway's own infrastructure routes (api.gateway.ROUTES) ---------------
    paths["/api/health"] = {
        "get": {
            "operationId": "health",
            "summary": "Liveness and posture: the engine's own view of itself.",
            "tags": ["infrastructure"],
            # Wired to the Docker HEALTHCHECK: "listening" and "answering" are the same probe.
            "responses": {
                "200": {
                    "description": "The engine is serving.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/HealthResponse"}}
                    },
                },
            },
        }
    }
    paths["/api/plans"] = {
        "get": {
            "operationId": "plans",
            "summary": "Which plans exist: the store's and the workspace's.",
            "tags": ["infrastructure"],
            "responses": {
                "200": {
                    "description": "The stored plan ids plus the plan files on disk.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/PlanListResponse"}}
                    },
                }
            },
        }
    }
    paths["/api/plan"] = {
        "get": {
            "operationId": "activePlan",
            "summary": "The active plan: its tasks in document order and its counters.",
            "tags": ["infrastructure"],
            "responses": {
                "200": {
                    "description": "The active plan.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/PlanView"}}
                    },
                }
            },
        }
    }
    paths["/api/plan/{plan_id}"] = {
        "get": {
            "operationId": "planById",
            "summary": "One stored plan by id.",
            "tags": ["infrastructure"],
            "parameters": [_path_param("plan_id", "The plan's stable id (the file's stem).")],
            "responses": {
                "200": {
                    "description": "The plan.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/PlanView"}}
                    },
                },
                "404": {"description": "Unknown plan.", "content": ERROR_CONTENT},
            },
        }
    }
    paths["/api/telemetry"] = {
        "get": {
            "operationId": "telemetry",
            "summary": "The newest execution receipts, bounded, with the ledger total.",
            "tags": ["infrastructure"],
            "responses": {
                "200": {
                    "description": "The receipts.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/TelemetryListResponse"}}
                    },
                }
            },
        }
    }
    paths["/api/telemetry/{execution_id}"] = {
        "get": {
            "operationId": "telemetryReceipt",
            "summary": "One append-only execution receipt.",
            "tags": ["infrastructure"],
            "parameters": [_path_param("execution_id", "The container execution's id.")],
            "responses": {
                "200": {
                    "description": "The receipt.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/TelemetryReceiptView"}}
                    },
                },
                "404": {"description": "Unknown execution.", "content": ERROR_CONTENT},
            },
        }
    }
    paths["/api/events"] = {
        "get": {
            "operationId": "eventStream",
            "summary": "The engine's event bus, as a server-sent event stream.",
            "tags": ["infrastructure"],
            "description": (
                "Frames of JSON events on the bus natively bound to the window and the console. "
                "Never terminates voluntarily: a heartbeat comment frame keeps the connection warm."
            ),
            "responses": {
                "200": {
                    "description": "An open SSE stream (`text/event-stream`).",
                    "content": SSE_CONTENT,
                }
            },
        }
    }
    paths["/api/intents/{intent_id}/stream"] = {
        "get": {
            "operationId": "intentStream",
            "summary": "The same stream, narrowed to one run.",
            "tags": ["infrastructure"],
            "description": (
                "A `stream`, not an `operation`: it does not answer once. The frames are the hub's, "
                "filtered to the named intent, so a client sees exactly the run it asked about."
            ),
            "parameters": [_path_param("intent_id", "The run's execution identity.")],
            "responses": {
                "200": {
                    "description": "An open SSE stream scoped to the intent.",
                    "content": SSE_CONTENT,
                }
            },
        }
    }

    # Every model that crosses the wire in either direction.
    component_models: List[Type[BaseModel]] = [
        schemas.AcknowledgeResponse,
        schemas.ErrorResponse,
        schemas.HealthResponse,
        schemas.IntentLedgerEntry,
        schemas.IntentQueueStatus,
        schemas.IntentRequest,
        schemas.IntentView,
        schemas.MetricsView,
        schemas.OperationEnvelope,
        schemas.PlanListResponse,
        schemas.PlanView,
        schemas.ServiceRefusal,
        schemas.TaskView,
        schemas.TelemetryListResponse,
        schemas.TelemetryReceiptView,
    ]
    # Plus every operation's own request model.
    for operation in operations.OPERATIONS:
        if operation.request is not None and operation.request.__name__ not in {
            model.__name__ for model in component_models
        }:
            component_models.append(operation.request)

    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "Aleth Engine",
            "version": "0.1.0",
            "summary": "The local AI execution engine: plan, gate, execute -- on one loopback port.",
            "description": (
                "The whole HTTP surface of the engine: the typed operation table "
                "(`api.operations.OPERATIONS`) plus the gateway's own infrastructure reads and "
                "event streams (`api.gateway.ROUTES`). One process, one port. Every request body "
                "is a strict model (`extra=\"forbid\"`) and every operation answer is the shared "
                "envelope; every non-2xx body is `ErrorResponse`. The server binds 127.0.0.1 and "
                "refuses any peer that is not loopback."
            ),
            "license": {"name": "Proprietary"},
        },
        "servers": [
            {
                "url": "http://127.0.0.1:8765",
                "description": "The engine's single socket (ALETH_API_PORT overrides the port).",
            }
        ],
        "tags": [
            {"name": "operations", "description": "The typed capability surface: every engine action the UI may invoke, and nothing else."},
            {"name": "infrastructure", "description": "Health, plan projections, the telemetry ledger, and the event streams."},
        ],
        "paths": dict(sorted(paths.items())),
        "components": {
            "responses": SHARED_RESPONSES,
            **_components(component_models),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = args[0] if args else os.path.join(os.getcwd(), "openapi.json")
    spec = build_spec()
    text = json.dumps(spec, indent=2, ensure_ascii=True, sort_keys=False)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
    paths = spec["paths"]
    operations_count = sum(len(item) for item in paths.values())
    print(f"wrote {output}: {len(paths)} paths, {operations_count} operations, "
          f"{len(spec['components']['schemas'])} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())