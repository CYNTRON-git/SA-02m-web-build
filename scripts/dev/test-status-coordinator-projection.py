# comment-mutation-proof-exempt: executes the shipped CGI projection (bash) against snapshots written in the broker's real layout and process state; accepts observable JSON results.
"""Run the shipped status.cgi coordinator projection against broker-layout snapshots.

Both ends of the coupling are pinned here: the snapshot is produced by the
broker's own `snapshot_document` (opt/sa02m-port-coordinator), the projection is
the bash extracted from status.cgi — no Python runs on the poll path any more.
Linux only (/proc/<pid>/stat and AF_UNIX): elsewhere the row is SKIPPED (exit
77 + QUALITY_SKIP_LOG), never passed.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'www/network_config/cgi-bin/status.cgi').read_text(encoding='utf-8')
# Everything from cache_is_fresh up to (not including) status_timeout_run — the
# same slice test-status-cache-metadata.sh sources.
FUNCTIONS = SOURCE.split('cache_is_fresh() {', 1)[1].split('status_timeout_run() {', 1)[0]
FUNCTIONS = 'cache_is_fresh() {' + FUNCTIONS


def _skip_off_linux():
    # The package import below needs fcntl: the guard must run before it.
    if sys.platform != 'linux' or not hasattr(socket, 'AF_UNIX') or not Path('/proc/self/stat').exists():
        reason = 'status-coordinator-projection: Linux /proc + AF_UNIX required; skipped on ' + sys.platform
        print('SKIP  ' + reason)
        log = os.environ.get('QUALITY_SKIP_LOG')
        if log:
            with open(log, 'a', encoding='utf-8') as fh:
                fh.write(reason + '\n')
        sys.exit(77)


_skip_off_linux()
sys.path.insert(0, str(ROOT / 'opt/sa02m-port-coordinator'))
from sa02m_ports.broker import snapshot_document  # noqa: E402

PROJECT = 'status_port_coordinator; printf "%s" "$STATUS_PORT_COORDINATOR"'


def run_bash(script, directory, env=None):
    full = {**os.environ, 'STATUS_PORT_COORDINATOR_DIR': str(directory), **(env or {})}
    return subprocess.run(['bash', '-c', FUNCTIONS + '\n' + script], capture_output=True,
                          text=True, env=full, check=True).stdout


def self_ticks():
    return Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]


class Projection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.root / 'control.sock'))

    def tearDown(self):
        self.sock.close()
        self.tmp.cleanup()

    def write(self, document):
        (self.root / 'status.json').write_text(json.dumps(document), encoding='utf-8')

    def project(self):
        return json.loads(run_bash(PROJECT, self.root))

    def document(self, ticks, **state):
        full = {'leases': {}, 'services': {}, **state}
        return snapshot_document(full, {'ports': {'/dev/COM1': ['mplc4.service'], '/dev/COM2': []}},
                                 os.getpid(), ticks)

    def test_live_and_retired_owner_preserve_restoration_failure(self):
        services = {'mplc4.service': {'phase': 'restore_failed', 'error': 'injected "quoted" failure'}}
        lease = {'id': 'abc', 'port': '/dev/COM1', 'key': '136-1', 'purpose': 'scan', 'pid': 42,
                 'services': ['mplc4.service'], 'phase': 'owned', 'error': ''}
        for alive in (True, False):
            self.write(self.document(self_ticks() if alive else '0', services=services, leases={'abc': lease}))
            result = self.project()
            self.assertEqual(result['available'], alive)
            self.assertEqual(result['services']['mplc4.service']['phase'], 'restore_failed')
            self.assertEqual(result['services']['mplc4.service']['error'], 'injected "quoted" failure')
            self.assertEqual(result['leases'][0]['port'], '/dev/COM1')
            self.assertEqual(result['configured_scopes']['/dev/COM1'], ['mplc4.service'])
            self.assertNotIn('broker_pid', result)
            self.assertNotIn('broker_start_ticks', result)
            self.assertIsInstance(result['timestamp'], float)

    def test_unreadable_or_foreign_snapshot_is_unavailable_never_projected(self):
        unavailable = {'available': False, 'error': 'snapshot_unavailable'}
        (self.root / 'status.json').write_text('{', encoding='utf-8')
        self.assertEqual(self.project(), unavailable)
        foreign = self.document(self_ticks())
        foreign['secret'] = 'must-not-project'
        self.write(foreign)
        result = self.project()
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(result, unavailable)
        (self.root / 'status.json').unlink()
        self.assertEqual(self.project(), unavailable)
        (self.root / 'status.json').write_text('{"timestamp": 1.0, "leases": [], "services": {}, "configured_scopes": {}, "broker_pid": 1, "broker_start_ticks": "0"}' + ' ' * 131072, encoding='utf-8')
        self.assertEqual(self.project(), unavailable)

    def test_socket_absence_and_zombie_state_mean_not_available(self):
        self.write(self.document(self_ticks()))
        self.assertTrue(self.project()['available'])
        self.sock.close()
        (self.root / 'control.sock').unlink()
        self.assertFalse(self.project()['available'])

    def test_services_answer_splices_valid_json_with_metadata(self):
        self.write(self.document(self_ticks()))
        out = run_bash('status_emit_snapshot \'{"ok":true,"services":[{"id":"mplc4"}]}\' "$((EPOCHSECONDS - 3))" 45',
                       self.root, {'STATUS_PART': 'services'})
        data = json.loads(out)
        self.assertEqual(data['services'], [{'id': 'mplc4'}])
        self.assertTrue(data['port_coordinator']['available'])
        self.assertEqual(data['_status_meta']['ttl_s'], 45)
        self.assertGreaterEqual(data['_status_meta']['age_s'], 3)
        out = run_bash('status_emit_snapshot \'{"cpu":1}\' "$EPOCHSECONDS" 0', self.root, {'STATUS_PART': 'priority'})
        self.assertNotIn('port_coordinator', json.loads(out))

    def test_poll_path_forks_no_interpreter(self):
        # The one fork budget the resource model allows here is stat(1); a
        # python3 or date on this path is the defect this row exists for.
        self.assertNotIn('python3', FUNCTIONS)
        self.assertNotIn('date +', FUNCTIONS)


if __name__ == '__main__':
    unittest.main()
