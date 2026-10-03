# -*- coding: utf-8 -*-
"""MCP Streamable HTTP: one JSON-RPC object per POST. Scope filters tools/list."""

import json

from . import __version__
from .guide import GUIDE, PROMPTS

PROTOCOL = "2025-03-26"


def resources():
    return [
        {"uri": "sa02m://guide", "name": "guide", "mimeType": "text/plain"},
        {"uri": "sa02m://openapi", "name": "openapi", "mimeType": "application/json"},
        {"uri": "sa02m://mqtt/topics", "name": "mqtt-topics", "mimeType": "application/json"},
        {"uri": "sa02m://rules/scenarios", "name": "scenarios", "mimeType": "application/json"},
        {"uri": "sa02m://user/tree", "name": "user-tree", "mimeType": "application/json"},
    ]


def handle(app, principal, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request"), 200
    mid = message.get("id", None)
    method = message.get("method") or ""
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if mid is None and method.startswith("notifications/"):
        return None, 202
    if method == "initialize":
        return _ok(mid, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": "sa02m-agent-api", "version": __version__},
            "instructions": GUIDE,
        }), 200
    if method == "ping":
        return _ok(mid, {}), 200
    if method == "tools/list":
        tools = []
        for op in app.ops:
            if not principal.allows(op.scope):
                continue
            tools.append({
                "name": op.tool,
                "description": op.summary,
                "inputSchema": op.schema,
            })
        return _ok(mid, {"tools": tools}), 200
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(mid, -32602, "arguments"), 200
        op = None
        for item in app.ops:
            if item.tool == name:
                op = item
                break
        if op is None or not principal.allows(op.scope):
            return _err(mid, -32602, "unknown or forbidden tool"), 200
        if op.long and "wait" not in args:
            # An MCP client has no job poller: long tools answer inline by default.
            args = dict(args, wait=True)
        status, body = app.invoke(principal, op, args)
        text = json.dumps(body, ensure_ascii=False)
        return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": status >= 400}), 200
    if method == "resources/list":
        return _ok(mid, {"resources": resources()}), 200
    if method == "resources/read":
        uri = params.get("uri") or ""
        text = app.read_resource(principal, uri)
        if text is None:
            return _err(mid, -32002, "resource"), 200
        return _ok(mid, {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}), 200
    if method == "prompts/list":
        return _ok(mid, {"prompts": [
            {"name": k, "description": v["description"]} for k, v in PROMPTS.items()
        ]}), 200
    if method == "prompts/get":
        name = params.get("name") or ""
        item = PROMPTS.get(name)
        if not item:
            return _err(mid, -32602, "prompt"), 200
        return _ok(mid, {"description": item["description"], "messages": [
            {"role": "user", "content": {"type": "text", "text": item["text"]}}
        ]}), 200
    return _err(mid, -32601, "method"), 200


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
