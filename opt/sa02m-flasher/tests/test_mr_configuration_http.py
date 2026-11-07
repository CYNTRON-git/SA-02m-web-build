"""Real HTTP/session/CSRF/configuration handler with recorded UART and lease boundaries."""
import ast
import os
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from contextlib import contextmanager
from unittest.mock import patch

from sa02m_flasher import device_config as dc, mr_configuration as config
from tests import test_csrf_dispatch as csrf
from tests.test_mr_configuration_transport import ConfigurationBus, DeferredAiBus
from tests.test_mr_readback_truth import device


class ConfigurationHttp(unittest.TestCase):
    def setUp(self):
        csrf.TestCsrfDispatch.setUp(self)
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), csrf._StubHandler)
        self.srv.context = self.ctx
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.t.start()
    tearDown = csrf.TestCsrfDispatch.tearDown
    _req = csrf.TestCsrfDispatch._req

    def install_handler(self, bus, release_failure=False):
        tree = ast.parse(csrf._SERVICE_SRC)
        handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Handler')
        nodes = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        nodes.update({n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)})
        self.events = []
        self.lock = threading.Lock()
        self.locks = {'COM4': self.lock, 'COM5': threading.Lock()}

        @contextmanager
        def lease(path, services, *, preserve_released, acquire_deadline=None, release_timeout=None):
            self.assertTrue(preserve_released)
            if acquire_deadline is not None:
                self.assertLessEqual(acquire_deadline - time.monotonic(), 45)
                self.assertEqual(release_timeout, 45)
            self.events.append('lease')
            try:
                yield
            finally:
                self.assertGreater(bus.closed, 0)
                self.events.append('release')
                if release_failure:
                    raise RuntimeError('Coordinator recovery pending')

        ns = {**csrf._NS, 'os': os, 'time': time, 'device_config': dc, 'port_lease': lease,
              '_device_config_port_lock': lambda port: self.locks[port]}
        names = ('_configuration_body_length', '_read_configuration_body', '_device_config_request',
                 '_handle_configuration_file', '_guard_no_active_flasher_job', '_dispatch',
                 '_read_json_body', '_run_device_config_modbus', '_handle_device_config_coil', '_handle_device_config_snapshot',
                 '_handle_device_config_holding')
        exec('from __future__ import annotations\n' + '\n'.join(csrf._src_of(nodes[name]) for name in names), ns)
        self.srv.RequestHandlerClass = type('ConfigurationHandler', (csrf._StubHandler,),
            {name: ns[name] for name in ('_dispatch', '_device_config_request', '_handle_configuration_file', '_run_device_config_modbus', '_handle_device_config_coil', '_handle_device_config_snapshot', '_handle_device_config_holding')})
        self.ctx.cfg.ports_map = {'COM4': str(self.sdir), 'COM5': str(self.sdir)}
        self.ctx.cfg.mplc_stop_services = []
        self.ctx.jobs.any_active_job = lambda: None
        self.ctx.jobs.active_job_on_port = lambda port: None
        self.addCleanup(patch.stopall)
        patch.object(dc, '_open_transport', return_value=(bus, bus.close)).start()

    def test_export_preview_apply_revalidate_under_one_lease_each(self):
        bus = ConfigurationBus(3)
        self.install_handler(bus)
        body = {'port': 'COM4', 'device': device(3)}
        status, exported = self._req('POST', '/device_config/export', csrf=csrf._CSRF, body=body)
        self.assertEqual(status, 200, exported)
        file = exported['configuration']
        file['settings']['ao']['channels'][0]['safe_raw'] = 100
        body.update(configuration=file, preview_only=True)
        status, preview = self._req('POST', '/device_config/import', csrf=csrf._CSRF, body=body)
        self.assertEqual(status, 200, preview)
        self.assertEqual(bus.writes, [])
        self.assertEqual([item['path'] for item in preview['changes']], ['ao.1.safe_raw'])
        body['preview_only'] = False
        status, result = self._req('POST', '/device_config/import', csrf=csrf._CSRF, body=body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result['ok'])
        self.assertEqual(bus.writes, [(503, 100)])
        self.assertEqual(bus.closed, 3)
        self.assertEqual(self.events, ['lease', 'release'] * 3)

    def test_auth_csrf_invalid_file_and_active_job_never_write(self):
        bus = ConfigurationBus(3)
        self.install_handler(bus)
        body = {'port': 'COM4', 'device': device(3), 'configuration': {}, 'preview_only': False}
        status, result = self._req('POST', '/device_config/import', cookie=False, body=body)
        self.assertEqual(status, 401)
        status, result = self._req('POST', '/device_config/import', body=body)
        self.assertEqual(result['error_code'], 'E_CSRF')
        self.assertFalse(self.events)
        for value in (True, '2', -1, 248):
            body['device']['address'] = value
            status, result = self._req('POST', '/device_config/import', csrf=csrf._CSRF, body=body)
            self.assertEqual(status, 400, result)
        self.assertFalse(bus.writes)

    def test_legacy_ai_full_file_is_readable_but_cannot_be_applied(self):
        bus = DeferredAiBus()
        self.install_handler(bus)
        body = {'port': 'COM4', 'device': device(7)}
        status, exported = self._req('POST', '/device_config/export', csrf=csrf._CSRF, body=body)
        self.assertEqual(status, 200, exported)
        self.assertTrue(exported['complete'])
        self.assertIn('ai.1.sensor_code', exported['configuration']['coverage']['unsupported'])
        body.update(configuration=exported['configuration'], allow_partial=True)
        for preview in (True, False):
            body['preview_only'] = preview
            status, result = self._req('POST', '/device_config/import', csrf=csrf._CSRF, body=body)
            self.assertEqual(status, 200, result)
            self.assertTrue(result['apply_blocked'])
            self.assertEqual(result['ok'], preview)
            self.assertFalse(result['complete'])
        self.assertFalse(bus.writes)
        self.assertEqual(self.events, ['lease', 'release'] * 3)

    def test_old_browser_cannot_mistake_legacy_type_command_for_success(self):
        bus = DeferredAiBus()
        self.install_handler(bus)
        body = {'port': 'COM4', 'device': device(7), 'reg': 400, 'value': 35}
        status, result = self._req('POST', '/device_config/holding', csrf=csrf._CSRF, body=body)
        self.assertEqual(status, 409, result)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'AI_TYPE_ACCEPTED_UNCONFIRMED')
        self.assertEqual(result['ai_write']['status'], 'accepted_unconfirmed')
        self.assertEqual(bus.writes, [(400, 35)])

    def test_restoration_failure_preserves_confirmed_bus_result(self):
        bus = ConfigurationBus(3)
        self.install_handler(bus, release_failure=True)
        file = config.export_configuration(bus.profile, '1.0.12.22', bus.words)
        file['settings']['ao']['channels'][0]['safe_raw'] = 100
        status, result = self._req('POST', '/device_config/import', csrf=csrf._CSRF,
            body={'port': 'COM4', 'device': device(3), 'configuration': file, 'preview_only': False})
        self.assertEqual(status, 200)
        self.assertFalse(result['ok'])
        self.assertEqual(result['applied'][0]['observed'], 100)
        self.assertIn('recovery', result['restoration_error'])
        self.assertEqual(self.events, ['lease', 'release'])

    def test_slow_partial_body_has_one_absolute_deadline(self):
        bus = ConfigurationBus()
        self.install_handler(bus)
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=4)
        try:
            headers = ('POST /device_config/import HTTP/1.1\r\nHost: localhost\r\n'
                f'Cookie: session_token={csrf._TOKEN}\r\nX-SA02M-CSRF: {csrf._CSRF}\r\n'
                'Content-Length: 100\r\n\r\n{')
            started = time.monotonic()
            connection.sendall(headers.encode())
            response = connection.recv(8192)
            self.assertIn(b'400', response)
            self.assertLess(time.monotonic() - started, 3.5)
            self.assertFalse(self.events)
        finally:
            connection.close()

    def test_same_port_lock_is_bounded_but_another_port_remains_available(self):
        bus = ConfigurationBus(3)
        self.install_handler(bus)
        first_result = []
        self.lock.acquire()
        worker = threading.Thread(target=lambda: first_result.append(self._req('POST', '/device_config/export',
            csrf=csrf._CSRF, body={'port': 'COM4', 'device': device(3)})))
        try:
            worker.start()
            started = time.monotonic()
            status, result = self._req('POST', '/device_config/export', csrf=csrf._CSRF,
                body={'port': 'COM5', 'device': device(3)})
            self.assertEqual(status, 200, result)
            self.assertLess(time.monotonic() - started, 1)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(first_result[0][0], 409)
            self.assertEqual(self.events, ['lease', 'release'])
        finally:
            self.lock.release()
            worker.join(4)

    def test_job_appearing_while_lock_waited_is_checked_again(self):
        bus = ConfigurationBus(3)
        self.install_handler(bus)
        calls = []
        def job():
            calls.append(1)
            return len(calls) >= 2
        self.ctx.jobs.any_active_job = job
        status, result = self._req('POST', '/device_config/export', csrf=csrf._CSRF,
            body={'port': 'COM4', 'device': device(3)})
        self.assertEqual(status, 409, result)
        self.assertFalse(self.events)
        self.assertFalse(bus.calls)

    def test_invalid_framing_and_json_are_rejected_before_uart_access(self):
        bus = ConfigurationBus()
        self.install_handler(bus)
        for headers in ({'Content-Length': '-1'}, {'Content-Length': '65537'}, {'Transfer-Encoding': 'chunked'}):
            status, result = self._req('POST', '/device_config/export', csrf=csrf._CSRF, extra=headers,
                body={'port': 'COM4', 'device': device()})
            self.assertEqual(status, 400, result)
        for raw in (b'[]', b'{"port":"COM4","port":"COM5"}', b'{"value":NaN}', b'{"value":"\xff"}'):
            connection = socket.create_connection(('127.0.0.1', self.port), timeout=4)
            try:
                headers = ('POST /device_config/import HTTP/1.1\r\nHost: localhost\r\n'
                    f'Cookie: session_token={csrf._TOKEN}\r\nX-SA02M-CSRF: {csrf._CSRF}\r\n'
                    f'Content-Length: {len(raw)}\r\n\r\n')
                connection.sendall(headers.encode() + raw)
                self.assertIn(b'400', connection.recv(8192))
            finally:
                connection.close()
        self.assertFalse(self.events)
        self.assertFalse(bus.calls)


if __name__ == '__main__':
    unittest.main()
