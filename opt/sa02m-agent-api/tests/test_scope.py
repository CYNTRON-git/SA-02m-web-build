# -*- coding: utf-8 -*-
"""Every operation refuses a short scope before any side effect."""

import json
import os
import tempfile
import unittest

from sa02m_agent_api.ops import register_all
from sa02m_agent_api.registry import OPS
from sa02m_agent_api.service import App
from sa02m_agent_api.tokens import RateLimiter


def _args_for(op):
    extra = {
        "system.status": {"part": "all"},
        "rules.get": {"id": "scene1"},
        "logs.journal": {"unit": "sa02m-agent-api", "lines": 10},
        "user.units.status": {"name": "demo"},
        "user.units.logs": {"name": "demo"},
        "mqtt.publish": {"device": "dev", "control": "on", "value": "1"},
        "hw.set": {"channel": "do1", "value": "1"},
        "rules.run": {"id": "scene1"},
        "rules.off": {"id": "scene1"},
        "rules.delete": {"id": "scene1"},
        "rules.replace": {"scenarios": [{"id": "scene1", "name": "a"}]},
        "rules.library": {"library": "function f() {}"},
        "mqtt.scan": {"port": "/dev/ttyS1", "baudrate": 9600},
        "gateway.ctrl": {"action": "start"},
        "web_update.apply": {"confirm_version": "1.0.7.1"},
        "nodered.nodes.install": {"module": "node-red"},
        "mplc.project.deploy": {"zip_b64": "YQ=="},
        "user.files.write": {"path": "a.txt", "text": "hi"},
        "user.pip": {"package": "requests"},
        "shell.exec": {"cmd": "true", "timeout": 5, "mode": "web"},
        "files.read": {"path": "/etc/hostname"},
        "files.write": {"path": "/etc/hostname", "text": "x"},
        "web_update.upload": {"file_b64": "YQ=="},
        "kernel.set": {"profile": "smp"},
        "cpu.profile": {"profile": "low"},
    }
    if op.name.startswith("services."):
        extra[op.name] = {"id": "mosquitto"}
    if op.name.startswith("user.units.") and op.name not in extra:
        extra[op.name] = {"name": "demo"}
    args = dict(extra.get(op.name) or {})
    if op.confirm:
        args["confirm"] = op.confirm
    return args


class ScopeMatrix(unittest.TestCase):
    def setUp(self):
        register_all()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SA02M_AGENT_TOKEN_DIRECT"] = "1"
        os.environ["SA02M_AGENT_TOKEN_FILE"] = os.path.join(self.tmp.name, "tokens.json")
        os.environ["SA02M_AGENT_RUNTIME"] = self.tmp.name
        os.environ["SA02M_AGENT_AUDIT"] = os.path.join(self.tmp.name, "audit.jsonl")
        self.app = App()
        self.app.store.path = os.environ["SA02M_AGENT_TOKEN_FILE"]
        self.app.limiter = RateLimiter(100000)
        self.app.admin_per_min = 100000
        self.app.inline_jobs = True
        self.app.audit_path = os.environ["SA02M_AGENT_AUDIT"]
        self.app.sink = lambda name: {"ok": True}
        self.full_row, self.full = self.app.store.issue("full", ["admin"], 1, True)
        self.read_row, self.read = self.app.store.issue("reader", ["read"], 1, False)
        self.none = self._bare()

    def tearDown(self):
        self.tmp.cleanup()

    def _bare(self):
        row, raw = self.app.store.build("none", ["read"], 1, False)
        row["scopes"] = []
        rows = self.app.store.load()
        rows.append(row)
        self.app.store.save(rows)
        return raw

    def _call(self, token, op, args):
        self.app.sides = []
        status, body = self.app.handle(
            "POST", op.rest_path,
            {"X-SA02M-Token": token},
            json.dumps(args).encode("utf-8"),
        )
        return status, body

    def test_every_op_has_a_scope_and_refuses_early(self):
        self.assertGreater(len(OPS), 40)
        for op in OPS:
            self.assertIn(op.scope, ("read", "control", "config", "admin"))
            short = self.none if op.scope == "read" else self.read
            status, body = self._call(short, op, _args_for(op))
            self.assertEqual(status, 403, op.name)
            self.assertEqual(body.get("need"), op.scope, op.name)
            self.assertEqual(self.app.sides, [], op.name)

    def test_allowed_scope_reaches_the_side(self):
        for op in OPS:
            status, body = self._call(self.full, op, _args_for(op))
            self.assertLess(status, 400, "%s %s" % (op.name, body))
            self.assertIn(op.name, self.app.sides, op.name)

    def test_confirm_and_dry_run_stop_before_the_side(self):
        for op in OPS:
            if not op.confirm:
                continue
            args = _args_for(op)
            args.pop("confirm", None)
            status, body = self._call(self.full, op, args)
            self.assertEqual(status, 409, op.name)
            self.assertEqual(self.app.sides, [], op.name)
            if op.mutating:
                args["confirm"] = op.confirm
                args["dry_run"] = True
                status, body = self._call(self.full, op, args)
                self.assertEqual(status, 200, op.name)
                self.assertTrue(body.get("dry_run"), op.name)
                self.assertEqual(self.app.sides, [], op.name)

    def test_files_need_root_capable_before_the_side(self):
        row, raw = self.app.store.issue("admin-only", ["admin"], 1, False)
        self.assertFalse(row["root_capable"])
        for name in ("files.read", "files.write"):
            op = next(item for item in OPS if item.name == name)
            status, body = self._call(raw, op, _args_for(op))
            self.assertEqual(status, 403, name)
            self.assertEqual(body.get("need"), "root_capable")
            self.assertEqual(self.app.sides, [], name)


if __name__ == "__main__":
    unittest.main()
