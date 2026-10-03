# -*- coding: utf-8 -*-
"""Operation registry. One record is a REST route and an MCP tool."""


class Op:
    def __init__(self, name, scope, summary, handler, schema=None,
                 mutating=False, confirm=None, long=False):
        self.name = name
        self.scope = scope
        self.summary = summary
        self.handler = handler
        self.schema = schema or {"type": "object", "additionalProperties": True}
        self.mutating = mutating
        self.confirm = confirm
        self.long = long

    @property
    def rest_path(self):
        return "/api/v1/" + self.name.replace(".", "/")

    @property
    def tool(self):
        return "sa02m_" + self.name.replace(".", "_")


OPS = []
_BY_NAME = {}


def add(op):
    if op.name in _BY_NAME:
        raise RuntimeError("duplicate op " + op.name)
    OPS.append(op)
    _BY_NAME[op.name] = op
    return op


def get(name):
    return _BY_NAME.get(name)


def reset():
    OPS[:] = []
    _BY_NAME.clear()
