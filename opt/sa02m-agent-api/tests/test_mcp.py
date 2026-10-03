# -*- coding: utf-8 -*-
"""MCP handshake: initialize, tools, one call, one resource."""

import json
import os
import tempfile
import unittest

from sa02m_agent_api.ops import register_all
from sa02m_agent_api.service import App
from sa02m_agent_api.tokens import RateLimiter


class McpHandshake(unittest.TestCase):
    def setUp(self):
        register_all()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SA02M_AGENT_TOKEN_DIRECT"] = "1"
        os.environ["SA02M_AGENT_TOKEN_FILE"] = os.path.join(self.tmp.name, "tokens.json")
        os.environ["SA02M_AGENT_RUNTIME"] = self.tmp.name
        self.app = App()
        self.app.store.path = os.environ["SA02M_AGENT_TOKEN_FILE"]
        self.app.limiter = RateLimiter(100000)
        self.app.admin_per_min = 100000
        self.app.inline_jobs = True
        self.app.sink = lambda name: {"ok": True}
        _row, self.read = self.app.store.issue("reader", ["read"], 1, False)

    def tearDown(self):
        self.tmp.cleanup()

    def _rpc(self, message):
        status, body = self.app.handle(
            "POST", "/mcp",
            {"X-SA02M-Token": self.read},
            json.dumps(message).encode("utf-8"),
        )
        if status == 202:
            self.assertEqual(body, b"")
            return status, None
        return status, body

    def test_handshake(self):
        status, init = self._rpc({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        self.assertEqual(status, 200)
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("sa02m", init["result"]["instructions"])

        status, listed = self._rpc({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        names = [t["name"] for t in listed["result"]["tools"]]
        self.assertIn("sa02m_system_status", names)
        self.assertNotIn("sa02m_shell_exec", names)
        self.assertNotIn("sa02m_network_apply", names)

        status, called = self._rpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "sa02m_system_status", "arguments": {"part": "all"}},
        })
        self.assertEqual(status, 200)
        self.assertFalse(called["result"]["isError"])
        self.assertIn("system.status", self.app.sides)

        # A long tool answers inline over MCP (no job poller on that side).
        self.app.inline_jobs = False
        _row, admin = self.app.store.issue("admin", ["admin"], 1, False)
        status, body = self.app.handle(
            "POST", "/mcp", {"X-SA02M-Token": admin},
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": "sa02m_shell_exec",
                                   "arguments": {"cmd": "true", "mode": "web"}}}).encode())
        self.assertEqual(status, 200)
        text = body["result"]["content"][0]["text"]
        self.assertNotIn("job_id", text)
        self.assertIn("shell.exec", self.app.sides)

        status, resource = self._rpc({
            "jsonrpc": "2.0", "id": 4, "method": "resources/read",
            "params": {"uri": "sa02m://guide"},
        })
        text = resource["result"]["contents"][0]["text"]
        self.assertIn("OpenAPI", text)

        status, _body = self._rpc({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        self.assertEqual(status, 202)


if __name__ == "__main__":
    unittest.main()
