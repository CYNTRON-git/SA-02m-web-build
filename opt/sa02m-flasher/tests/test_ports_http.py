"""Real HTTP dispatch/auth/CSRF and port mutation handlers with fake leases."""
import ast
import json
import os
import types
import unittest
from unittest.mock import MagicMock

from tests import test_csrf_dispatch as csrf
_StubHandler, _NS, _src_of, _SERVICE_SRC, _CSRF = (csrf._StubHandler, csrf._NS, csrf._src_of, csrf._SERVICE_SRC, csrf._CSRF)


class PortsHttp(unittest.TestCase):
    setUp = csrf.TestCsrfDispatch.setUp
    tearDown = csrf.TestCsrfDispatch.tearDown
    _req = csrf.TestCsrfDispatch._req

    def prepare(self):
        tree = ast.parse(_SERVICE_SRC)
        handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        nodes = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        nodes.update({n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)})
        self.leases = MagicMock()
        ns = {**_NS, "os": os, "mplc_lease": self.leases, "_guard_no_active_flasher_job": lambda *a, **kw: None,
              "_unit_display_name": lambda s: s, "port_occupants": lambda p: [], "port_poll_service_labels": lambda p: []}
        names = ("_read_json_body", "_handle_ports_release", "_handle_ports_restore", "_describe_port", "_handle_ports", "_truthy_query")
        exec("from __future__ import annotations\n" + "\n".join(_src_of(nodes[n]) for n in names), ns)

        class ActualPortHandler(_StubHandler):
            _handle_ports_release = ns["_handle_ports_release"]
            _handle_ports_restore = ns["_handle_ports_restore"]

            def _describe_port(self, ctx, port):
                if port not in ctx.cfg.ports_map:
                    raise ValueError("unknown port")
                return {"key": port, "reserved": True, "busy_pids": []}

        self.srv.RequestHandlerClass = ActualPortHandler
        self.actual_port_methods = ns
        self.ctx.cfg.ports_map = {"COM1": "/dev/COM1"}
        self.ctx.cfg.mplc_stop_services = []
        self.leases.release_pollers.return_value = {"failed": [], "stopped_now": []}
        self.leases.restore_pollers.return_value = {"failed": [], "restarted": []}

    def test_quick_and_full_get_project_pending_recovery_without_manual_reserve(self):
        self.prepare()
        self.ctx.cfg.ports_labels = {}
        self.ctx.jobs.active_job_on_port = lambda key: None
        self.leases.is_reserved.return_value = False
        self.leases.configured_scope.return_value = ["mplc4.service"]
        self.leases.released_services_for.return_value = []
        recovery = {"restore_pending": True, "restore_services": ["mplc4.service"],
                    "restore_error": "start failed", "coordinator_available": False}
        self.leases.restoration_status.return_value = recovery
        for name in ("_describe_port", "_handle_ports"):
            setattr(self.srv.RequestHandlerClass, name, self.actual_port_methods[name])
        for path in ("/ports?quick=1", "/ports"):
            status, payload = self._req("GET", path)
            self.assertEqual(status, 200, payload)
            port = payload["ports"][0]
            self.assertFalse(port["reserved"])
            for key, value in recovery.items():
                self.assertEqual(port[key], value)

    def test_invalid_port_is_rejected_before_both_global_mutations(self):
        self.prepare()
        for route in ("release", "restore"):
            for body in ({}, {"port": "COM99"}, {"port": {"bad": 1}}):
                status, payload = self._req("POST", "/ports/" + route, csrf=_CSRF, body=body)
                self.assertEqual(status, 400, payload)
        self.leases.release_pollers.assert_not_called()
        self.leases.restore_pollers.assert_not_called()

    def test_auth_and_csrf_precede_mutation(self):
        self.prepare()
        for route in ("release", "restore"):
            self._req("POST", "/ports/" + route, cookie=False, csrf=_CSRF, body={"port": "COM1"})
            self._req("POST", "/ports/" + route, body={"port": "COM1"})
        self.leases.release_pollers.assert_not_called()
        self.leases.restore_pollers.assert_not_called()

    def test_partial_failure_is_not_overridden_by_stopped_services(self):
        self.prepare()
        self.leases.release_pollers.return_value = {"failed": ["COM4 busy"], "stopped_now": ["mplc4.service"]}
        status, payload = self._req("POST", "/ports/release", csrf=_CSRF, body={"port": "COM1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])

    def test_empty_scope_success_keeps_reserved_status(self):
        self.prepare()
        status, payload = self._req("POST", "/ports/release", csrf=_CSRF, body={"port": "COM1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["port"]["reserved"])
