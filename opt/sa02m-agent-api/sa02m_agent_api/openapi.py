# -*- coding: utf-8 -*-
"""OpenAPI 3.1 built from the registry. No second list of routes."""

from . import __version__


def build(ops):
    paths = {}
    for op in ops:
        post = {
            "operationId": op.name,
            "summary": op.summary,
            "x-sa02m-scope": op.scope,
            "x-sa02m-mutating": bool(op.mutating),
            "requestBody": {
                "required": False,
                "content": {"application/json": {"schema": op.schema}},
            },
            "responses": {"200": {"description": "ok"}, "403": {"description": "scope"}},
        }
        if op.confirm:
            post["x-sa02m-confirm"] = op.confirm
            post["responses"]["409"] = {"description": "confirm_required"}
        if op.long:
            post["x-sa02m-long"] = True
            post["description"] = ("Long operation: answers {job_id}; poll "
                                   "/api/v1/jobs/{id} or pass \"wait\": true.")
        item = {"post": post}
        if not op.mutating:
            # Read ops also answer GET; the query string carries the arguments.
            item["get"] = {
                "operationId": op.name + ".get",
                "summary": op.summary,
                "x-sa02m-scope": op.scope,
                "parameters": [
                    {"name": name, "in": "query", "required": False,
                     "schema": spec if isinstance(spec, dict) else {"type": "string"}}
                    for name, spec in ((op.schema or {}).get("properties") or {}).items()
                ],
                "responses": {"200": {"description": "ok"}, "403": {"description": "scope"}},
            }
        paths[op.rest_path] = item
    paths["/api/v1/jobs/{id}"] = {
        "get": {
            "operationId": "jobs.get",
            "summary": "Status and result of a long operation (owner token only)",
            "x-sa02m-scope": "read",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string", "pattern": "^[0-9a-f]{16}$"}}],
            "responses": {"200": {"description": "ok"}, "404": {"description": "not_found"}},
        }
    }
    paths["/api/v1/jobs/{id}/events"] = {
        "get": {
            "operationId": "jobs.events",
            "summary": "SSE stream for one job",
            "x-sa02m-scope": "read",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string", "pattern": "^[0-9a-f]{16}$"}}],
            "responses": {"200": {"description": "text/event-stream"}},
        }
    }
    paths["/api/v1/events"] = {
        "get": {
            "operationId": "events",
            "summary": "SSE stream of job events",
            "x-sa02m-scope": "read",
            "responses": {"200": {"description": "text/event-stream"}},
        }
    }
    paths["/health"] = {
        "get": {
            "operationId": "health",
            "summary": "Liveness, no token",
            "security": [],
            "responses": {"200": {"description": "ok"}},
        }
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "SA-02m Agent API", "version": __version__},
        "components": {
            "securitySchemes": {
                "token": {"type": "apiKey", "in": "header", "name": "X-SA02M-Token"}
            }
        },
        "security": [{"token": []}],
        "paths": paths,
    }
