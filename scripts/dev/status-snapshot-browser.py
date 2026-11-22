#!/usr/bin/env python3
# comment-mutation-proof-exempt: renders the shipped dashboard in Chromium and verifies real DOM updates, one snapshot transport, aging, errors and lifecycle cancellation.
"""Chromium integration for the ready dashboard snapshot; HTTP data is synthetic."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlparse

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    print('SKIP: Python Playwright and Chromium are required')
    sys.exit(77)

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT/'www/network_config'
state = {'revision': 1, 'error': False, 'old': False, 'warming': False, 'requests': [], 'activity': []}


def packet():
    now = time.time()
    measured = int(now) + state['revision']
    parts = {'blocks': {'time': 0}}
    for part in ('priority', 'storage', 'uptime', 'network', 'load', 'system', 'services', 'hardware', 'rs485'):
        parts[part] = {'_status_meta': {'measured_at': measured, 'age_s': 100 if state['old'] else 0, 'ttl_s': 6, 'sample_started_uptime': measured}}
    parts['priority'].update(cpu_usage=12 if state['revision']==1 else 77, temp_c=45, ram_total_kb=524288,
                             ram_used_kb=131072, ram_free_kb=393216, ram_pct=25, swap_total_kb=0,
                             disk_total_kb=8000000, disk_used_kb=2000000, disk_free_kb=6000000, disk_pct=25)
    parts['network'].update(eth0_ip='192.0.2.135', eth0_operstate='up', eth0_mac='02:00:00:00:00:01')
    parts['coordinator'] = {'port_coordinator': {'available': True, 'leases': [], 'services': {}},
                            '_status_meta': {'measured_at': measured, 'age_s': 0, 'ttl_s': 6, 'sample_started_uptime': measured}}
    if state['warming']:
        parts.pop('network')
    return {'served_at': now, 'served_uptime': now, 'snapshot': {'schema': 1, 'boot_id': 'fixture', 'generated_at': now, 'generated_uptime': now, 'parts': parts,
                                         'errors': {'priority': 'collection_failed'} if state['error'] else {}}}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, *_):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == '/navigation-check.html':
            encoded = b'<!doctype html><title>Navigation fixture</title>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if '/cgi-bin/' in url.path or url.path.startswith('/api/'):
            state['requests'].append(self.path)
            if url.path.endswith('/status_snapshot.cgi'):
                state['activity'].append(self.headers.get('X-SA02M-Dashboard'))
            value = packet() if url.path.endswith('/status_snapshot.cgi') else {'ok': True}
            encoded = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        else:
            super().do_GET()


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = 'http://127.0.0.1:'+str(server.server_port)
out = ROOT/'.ai-dev/worklogs'
out.mkdir(parents=True, exist_ok=True)
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1600, 'height': 1000})
        context.add_cookies([{'name': 'session_token', 'value': 'test', 'url': origin}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(origin+'/index.html')
        page.wait_for_function("typeof _statusSnapshotMode !== 'undefined' && _statusSnapshotMode && backgroundLoaded.main")
        assert '12' in page.locator('#cpu-val').inner_text()
        assert '25' in page.locator('#ram-pct').inner_text()
        assert not any('/status.cgi?' in request for request in state['requests']), state['requests']
        # Mirror an in-progress flasher's navigation guard without starting any
        # flash job. A trusted click permits the browser's real beforeunload UI.
        page.locator('#cpu-val').click()
        page.evaluate("""window.cancelNavigation = event => {
          event.preventDefault(); event.returnValue = '';
        }; window.addEventListener('beforeunload', window.cancelNavigation);""")
        dialogs = []
        def stay(dialog):
            dialogs.append(dialog.type)
            dialog.dismiss()
        page.once('dialog', stay)
        try:
            page.goto(origin+'/navigation-check.html', timeout=5000)
            raise AssertionError('navigation must be cancelled by the user choosing to stay')
        except PlaywrightError as error:
            assert 'ERR_ABORTED' in str(error), str(error)
        assert dialogs == ['beforeunload'], dialogs
        assert page.url == origin+'/index.html'
        assert page.evaluate('statusPageActive() && _statusSnapshotMode && Boolean(_statusPollTimers.snapshot)'), \
            'cancelled navigation must leave the visible dashboard polling'
        page.evaluate("window.removeEventListener('beforeunload', window.cancelNavigation)")
        page.screenshot(path=str(out/'status-snapshot-dark.png'), full_page=True)
        state['revision'] = 2
        page.wait_for_function("document.getElementById('cpu-val').textContent.includes('77')", timeout=8500)
        state['old'] = True
        page.evaluate('fetchStatusSnapshot(true)')
        page.wait_for_function("statusFreshness.priority.state === 'stale'")
        state['old'] = False; state['error'] = True
        page.evaluate('fetchStatusSnapshot(true)')
        page.wait_for_function("statusFreshness.priority.state === 'error'")
        state['error'] = False; state['warming'] = True
        page.evaluate('fetchStatusSnapshot(true)')
        page.wait_for_function("statusFreshness.network.warming === true && statusFreshness.priority.state === 'fresh'")
        assert page.locator('#dashboard-poll-alert, #port-operation-status').count() == 0
        assert page.locator('[data-status-parts="network"]').first.get_attribute('data-status-state') == 'warming'
        assert 'Подготовка данных' in page.locator('[data-status-parts="network"]').first.get_attribute('title')
        page.evaluate("document.documentElement.setAttribute('data-theme','light');window.sa02mI18n.setLang('en')")
        page.wait_for_function("document.querySelector('[data-status-parts=network]').title.includes('Preparing data')")
        page.set_viewport_size({'width': 500, 'height': 900})
        page.screenshot(path=str(out/'status-snapshot-light-narrow.png'), full_page=True)
        assert 'Preparing data' in page.locator('[data-status-parts="network"]').first.get_attribute('title')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.evaluate("switchTab('network')")
        page.wait_for_function("!statusPageActive() && !Boolean(_statusPollTimers.snapshot)")
        before = len(state['activity'])
        page.wait_for_timeout(6500)
        assert len(state['activity']) == before, 'another tab must not keep the dashboard lease alive'
        state['warming'] = False
        state['old'] = True
        page.evaluate("switchTab('dashboard')")
        page.wait_for_function("_statusSnapshotMode && statusFreshness.priority.state === 'stale'")
        assert len(state['activity']) == before + 1, 'return must start exactly one immediate request'
        assert '77' in page.locator('#cpu-val').inner_text(), 'old cached values remain visible'
        state['old'] = False
        page.wait_for_function("statusFreshness.priority.state === 'fresh'", timeout=8500)
        assert all(header == 'active' for header in state['activity']), state['activity']
        page.evaluate("""window.addEventListener('pagehide', () => {
          sessionStorage.setItem('snapshot-departure', JSON.stringify({
            mode: _statusSnapshotMode, timer: Boolean(_statusPollTimers.snapshot)
          }));
        });""")
        page.goto(origin+'/navigation-check.html')
        assert json.loads(page.evaluate("sessionStorage.getItem('snapshot-departure')")) == {'mode': False, 'timer': False}
        page.go_back()
        page.wait_for_function("_statusSnapshotMode && Boolean(_statusPollTimers.snapshot)")
        page.evaluate('teardownStatusPolling()')
        assert not errors, errors
        print(json.dumps({'ok': True, 'snapshot_requests': sum('status_snapshot.cgi' in r for r in state['requests']),
                          'legacy_status_requests': sum('/status.cgi?' in r for r in state['requests']),
                          'activity_headers': len(state['activity']), 'navigation_idle_ms': 6500,
                          'cancelled_beforeunload': len(dialogs), 'confirmed_pagehide': True,
                          'page_errors': errors}))
        browser.close()
finally:
    server.shutdown()
    server.server_close()
