"""Actual HTTP journal clear preserves chart/export values and auth boundaries."""

import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
import time

import pytest

from sa02m_devices import api, device_events, event_journal_state as journal
from tests.test_events_clear_continuity import _seed_alarm
from tests.journal_support import private_authority


@pytest.fixture
def http_journal(tmp_path, monkeypatch, private_authority):
    token, csrf = "a" * 64, "c" * 64
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    (sessions / digest).write_text("2000000000 admin\n")
    (sessions / (digest + ".csrf")).write_text(csrf)
    path = tmp_path / "active.db"
    monkeypatch.setenv("STAND_DEVICES_HISTORY_DB", str(path))
    monkeypatch.setenv("STAND_DEVICES_EVENTS_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(device_events, "emmc_staging_path", lambda: tmp_path / "no-staging.db")
    domain = journal.domain_for()
    journal.bootstrap(domain, ready=True)
    _seed_alarm(path, time.time() - 100)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.DevicesAPIHandler)
    server.session_dir = str(sessions)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()

    def request(route, *, method="GET", authorized=True, csrf_header=True):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Cookie": "session_token=" + token} if authorized else {}
        if csrf_header:
            headers["X-SA02M-CSRF"] = csrf
        try:
            conn.request(method, "/api/devices/" + route, headers=headers)
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if "json" in response.getheader("Content-Type", "") else body
        finally:
            conn.close()

    yield path, domain, request
    server.shutdown()
    server.server_close()
    thread.join(5)
    assert not thread.is_alive()


def test_http_clear_keeps_exact_chart_and_export_values(http_journal):
    _, _, request = http_journal
    route = "history?kind=carel&device_id=carel-COM3-1&metric=supply_temp&range=1h"
    export = "history/export?kind=carel&device_id=carel-COM3-1&metric=supply_temp&range=1h&format=txt"
    status, before = request(route)
    assert status == 200 and before["events"]
    assert before["series"][0]["points"]
    assert {point[1] for point in before["series"][0]["points"]} == {26.5}
    status, text_before = request(export)
    assert status == 200
    data_before = [line for line in text_before.splitlines() if not line.startswith("#")]
    assert len(data_before) >= 2
    assert request("events/clear", method="POST") == (200, {"ok": True, "deleted": 1})
    status, journal_read = request("events")
    assert status == 200 and journal_read["events"] == []
    status, after = request(route)
    assert status == 200 and after["events"] == []
    assert after["series"] == before["series"]
    status, text_after = request(export)
    assert status == 200
    assert [line for line in text_after.splitlines() if not line.startswith("#")] == data_before


@pytest.mark.parametrize("authorized,csrf_header", [(False, True), (True, False)])
def test_http_refusal_changes_neither_rows_nor_generation(http_journal, authorized, csrf_header):
    _, domain, request = http_journal
    before = journal.read_atomic(domain)
    status, body = request("events/clear", method="POST", authorized=authorized, csrf_header=csrf_header)
    if not authorized:
        assert status == 401 and body["error"] == "unauthorized"
    else:
        assert status == 200 and body["error_code"] == "E_CSRF"
    assert journal.read_atomic(domain) == before
    assert len(request("events")[1]["events"]) == 1


def test_http_not_ready_and_corrupt_state_fail_without_hiding_samples(http_journal):
    _, domain, request = http_journal
    journal.set_ready(domain, False)
    assert request("events")[0] == 200
    assert request("events/clear", method="POST")[0] == 500
    (domain.directory / "state.json").write_bytes(b"corrupt")
    assert request("events") == (503, {"ok": False, "error": "journal_unavailable"})
    assert request("events/clear", method="POST")[0] == 500
    status, history = request("history?kind=carel&device_id=carel-COM3-1&metric=supply_temp")
    assert status == 200 and history["events"] == []
    assert {point[1] for point in history["series"][0]["points"]} == {26.5}


def test_http_archive_busy_has_original_body_and_unchanged_state(http_journal, monkeypatch):
    path, domain, request = http_journal
    monkeypatch.setenv("STAND_DEVICES_EVENTS_CLEAR_BUSY_S", "0.1")
    before = journal.read_atomic(domain)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert request("events/clear", method="POST") == (503, {"ok": False, "error": "busy"})
    finally:
        holder.close()
    assert journal.read_atomic(domain) == before
    assert len(request("events")[1]["events"]) == 1
