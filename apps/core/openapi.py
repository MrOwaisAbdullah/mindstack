"""OpenAPI 3.1 document generator.

We walk `apps.core.registry.registry`, ask each Pydantic model for its JSON
Schema (Pydantic v2's `model_json_schema()` emits 2020-12, which OpenAPI 3.1
adopts verbatim — no translation layer), hoist the schemas under
`components.schemas/`, and rewrite refs to point there.

No third-party dependency. Students can read every line of how the spec is
built, which matches the boilerplate's transparency goal.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from apps.core.registry import EndpointSpec, registry


def build_openapi(
    *,
    version: str = "v1",
    title: str | None = None,
    api_version: str = "1.0.0",
    server_url: str = "/api",
) -> dict[str, Any]:
    """Build the OpenAPI 3.1 doc for one API version.

    `server_url` is the URL prefix under which endpoints are served. The doc
    serves the path-relative form by default so it works behind any reverse
    proxy without rewriting.

    Every registered endpoint of this version appears. There used to be an
    `exclude_slugs` parameter, fed the admin-only set so hidden endpoints
    stayed out of the published spec; that flag was removed before launch
    (see `apps.core.models.EndpointFlag`) and nothing else ever passed it.

    Schemas are namespaced per-endpoint (`<slug>_<version>_<ClassName>`) so
    two endpoints with `class Input(BaseModel)` don't collide in
    `components.schemas/`.
    """
    specs = [s for s in registry.all() if s.version == version]

    components_schemas: dict[str, Any] = {}
    paths: dict[str, Any] = {}

    for spec in specs:
        prefix = f"{spec.slug}_{spec.version}_"
        _add_model(components_schemas, spec.input_model, prefix=prefix)
        _add_model(components_schemas, spec.output_model, prefix=prefix)
        path_key = f"/{spec.version}/{spec.slug}"
        paths[path_key] = {
            spec.method.lower(): _operation(spec, prefix=prefix),
        }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": title or "API",
            "version": api_version,
            "description": (
                "Auto-generated from the endpoint registry. "
                "Edit endpoints, not this document."
            ),
        },
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {
            "schemas": components_schemas,
            "responses": _shared_responses(),
        },
    }


def _operation(spec: EndpointSpec, *, prefix: str) -> dict[str, Any]:
    in_ref = f"#/components/schemas/{prefix}{spec.input_model.__name__}"
    out_ref = f"#/components/schemas/{prefix}{spec.output_model.__name__}"
    op: dict[str, Any] = {
        "operationId": f"{spec.version}_{spec.slug}",
        "summary": spec.description or spec.slug,
        "tags": list(spec.tags) or [spec.version],
        "deprecated": spec.deprecated,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": in_ref}}},
        },
        "responses": {
            "200": {
                "description": "Success",
                "content": {"application/json": {"schema": {"$ref": out_ref}}},
            },
            "400": {"$ref": "#/components/responses/InvalidJson"},
            "422": {"$ref": "#/components/responses/InputValidationError"},
            "500": {"$ref": "#/components/responses/InternalError"},
        },
    }
    return op


def _add_model(
    components_schemas: dict[str, Any], model: type[BaseModel], *, prefix: str
) -> None:
    """Hoist a model + every $defs entry it produced into components.schemas.

    The per-endpoint prefix is folded into the ref template so refs that
    Pydantic emits inside the schema body point to the namespaced keys.
    """
    ref_template = f"#/components/schemas/{prefix}{{model}}"
    schema = model.model_json_schema(ref_template=ref_template)
    defs = schema.pop("$defs", None) or {}
    components_schemas[f"{prefix}{model.__name__}"] = schema
    for name, sub in defs.items():
        components_schemas[f"{prefix}{name}"] = sub


def _shared_responses() -> dict[str, Any]:
    error_schema = {
        "type": "object",
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "required": ["error"],
    }
    return {
        "InvalidJson": {
            "description": "Body is not valid JSON or not a JSON object.",
            "content": {"application/json": {"schema": error_schema}},
        },
        "InputValidationError": {
            "description": "Input failed Pydantic validation.",
            "content": {"application/json": {"schema": error_schema}},
        },
        "InternalError": {
            "description": "The endpoint raised an unhandled exception.",
            "content": {"application/json": {"schema": error_schema}},
        },
    }
