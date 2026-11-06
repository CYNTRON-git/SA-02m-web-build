# -*- coding: utf-8 -*-
"""Exercise job isolation through real tokens, routing, workers and SSE queues."""

import json
import os
import queue
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest import mock

from sa02m_agent_api.events import Bus
from sa02m_agent_api.service import App


class EventFixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmp_path = tmp.name
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "SA02M_AGENT_TOKEN_DIRECT": "1",
            "SA02M_AGENT_TOKEN_FILE": os.path.join(tmp.name, "tokens.json"),
            "SA02M_AGENT_RUNTIME": tmp.name,
            "SA02M_AGENT_AUDIT": os.path.join(tmp.name, "audit.jsonl"),
            "SA02M_SESSION_DIR": os.path.join(tmp.name, "sessions"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.app = App()
        self.reader, self.read_token = self.app.store.issue("reader", ["read"], 1)
        self.admin, self.admin_token = self.app.store.issue("admin", ["admin"], 1)
        self.workers = {}
        publish = self.app.bus.publish

        def observed_publish(event, *args, **kwargs):
            publish(event, *args, **kwargs)
            if event.get("event") in ("done", "error"):
                self.workers[event["job_id"]][1].set()

        # Signal only after the real Bus has considered every subscriber.
        observer = mock.patch.object(self.app.bus, "publish", side_effect=observed_publish)
        observer.start()
        self.addCleanup(observer.stop)
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        for release, finished in self.workers.values():
            release.set()
            self.assertTrue(finished.wait(5), "fixture worker did not finish")

    def start_job(self, owner, result=None, error=False):
        release, finished = threading.Event(), threading.Event()

        def work():
            if not release.wait(5):
                raise TimeoutError("fixture was not released")
            if error:
                raise ValueError("synthetic private error")
            return result

        jid = self.app.jobs.start(owner["id"], work)
        self.workers[jid] = release, finished
        return jid

    def finish_job(self, jid):
        release, finished = self.workers[jid]
        release.set()
        self.assertTrue(finished.wait(5), "fixture completion was not published")

    def request(self, token, path):
        return self.app.handle("GET", path, {"X-SA02M-Token": token})

    def subscribe(self, token, path="/api/v1/events"):
        kind, q = self.request(token, path)
        self.assertEqual(kind, "sse", q)
        self.addCleanup(self.app.bus.unsubscribe, q)
        return q

    def assert_empty(self, q):
        try:
            event = q.get_nowait()
        except queue.Empty:
            return
        self.fail("unexpected queued event: %r" % event)


class EventRoutes(EventFixture, unittest.TestCase):
    def test_general_stream_never_receives_foreign_job_and_preserves_own_payload(self):
        read_q = self.subscribe(self.read_token, "/api/v1/events?owner=" + self.admin["id"])
        admin_q = self.subscribe(self.admin_token)
        result = {"ok": True, "synthetic_private_result": "ADMIN-ONLY-SENTINEL"}
        foreign = self.start_job(self.admin, result)
        self.finish_job(foreign)
        self.assertEqual(admin_q.get_nowait(), {"event": "start", "job_id": foreign})
        self.assertEqual(admin_q.get_nowait(), {
            "event": "done", "job_id": foreign, "result": result,
        })
        for suffix in ("", "/events"):
            self.assertEqual(self.request(self.read_token, "/api/v1/jobs/" + foreign + suffix),
                             (404, {"ok": False, "error": "not_found"}))
        self.assert_empty(read_q)

        own_result = {"ok": True, "synthetic_private_result": "READER-ONLY-SENTINEL"}
        own = self.start_job(self.reader, own_result)
        self.finish_job(own)
        self.assertEqual(read_q.get_nowait(), {"event": "start", "job_id": own})
        self.assertEqual(read_q.get_nowait(), {
            "event": "done", "job_id": own, "result": own_result,
        })
        self.assert_empty(admin_q)
        self.assertEqual(self.request(self.read_token, "/api/v1/jobs/" + own), (200, {
            "ok": True, "job": {"id": own, "status": "done", "result": own_result},
        }))

    def test_job_stream_contains_only_requested_job_after_initial_state(self):
        result = {"ok": True, "value": "requested job"}
        own = self.start_job(self.reader, result)
        q = self.subscribe(self.read_token, "/api/v1/jobs/" + own + "/events")
        self.assertEqual(q.get_nowait(), {
            "event": "state", "job_id": own, "status": "running", "result": None,
        })
        for owner in (self.reader, self.admin):
            other = self.start_job(owner, {"ok": True, "value": "unrelated job"})
            self.finish_job(other)
        self.app.bus.publish({"event": "mqtt", "value": "public fixture"})
        self.assert_empty(q)
        self.finish_job(own)
        self.assertEqual(q.get_nowait(), {"event": "done", "job_id": own, "result": result})
        self.assert_empty(q)

    def test_error_events_use_the_same_owner_and_job_boundaries(self):
        general = self.subscribe(self.read_token)
        own = self.start_job(self.reader, error=True)
        specific = self.subscribe(self.read_token, "/api/v1/jobs/" + own + "/events")
        specific.get_nowait()  # initial state
        self.assertEqual(general.get_nowait(), {"event": "start", "job_id": own})
        for owner in (self.admin, self.reader):
            other = self.start_job(owner, error=True)
            self.finish_job(other)
            self.assert_empty(specific)
            if owner == self.reader:
                self.assertEqual(general.get_nowait(), {"event": "start", "job_id": other})
                self.assertEqual(general.get_nowait(), {
                    "event": "error", "job_id": other,
                    "result": {"ok": False, "error": "ValueError"},
                })
            self.assert_empty(general)
        self.finish_job(own)
        expected = {"event": "error", "job_id": own,
                    "result": {"ok": False, "error": "ValueError"}}
        self.assertEqual(general.get_nowait(), expected)
        self.assertEqual(specific.get_nowait(), expected)

    def test_foreign_burst_cannot_fill_queue_or_drop_own_completion(self):
        q = self.subscribe(self.read_token)
        for _ in range(55):  # 110 events exceed the 100-event queue.
            foreign = self.start_job(self.admin, {"ok": True, "value": "foreign"})
            self.finish_job(foreign)
        self.assert_empty(q)
        own = self.start_job(self.reader, {"ok": True})
        self.finish_job(own)
        self.assertEqual(q.get_nowait(), {"event": "start", "job_id": own})
        self.assertEqual(q.get_nowait(), {
            "event": "done", "job_id": own, "result": {"ok": True},
        })

    def test_public_events_preserved_and_subscription_capacity_released(self):
        queues = [self.subscribe(self.read_token) for _ in range(8)]
        self.assertEqual(self.request(self.read_token, "/api/v1/events"),
                         (503, {"ok": False, "error": "busy"}))
        public = {"event": "mqtt", "owner": self.admin["id"], "value": "public fixture"}
        self.app.bus.publish(public)
        for q in queues:
            self.assertEqual(q.get_nowait(), public)
        self.app.bus.unsubscribe(queues[0])
        replacement = self.subscribe(self.read_token)
        self.app.bus.publish(public)
        self.assert_empty(queues[0])
        self.assertEqual(replacement.get_nowait(), public)


class CompletionLock:
    """Keep the real lock and report whether the worker had to wait for it."""
    def __init__(self, lock):
        self.lock = lock
        self.attempted = threading.Event()
        self.contended = False

    def __enter__(self):
        if threading.current_thread().name.startswith("agent-job-"):
            acquired = self.lock.acquire(blocking=False)
            self.contended = not acquired
            self.attempted.set()
            if acquired:
                return self
        self.lock.acquire()
        return self

    def __exit__(self, *_exc):
        self.lock.release()


class EventOrdering(EventFixture, unittest.TestCase):
    def complete_at_boundary(self, jid, lock):
        release, finished = self.workers[jid]
        release.set()
        self.assertTrue(lock.attempted.wait(5), "worker did not reach its state transition")
        if not lock.contended:
            self.assertTrue(finished.wait(5), "unblocked worker did not publish completion")

    def check_subscription_boundary(self, after_registration):
        for error in (False, True):
            with self.subTest(error=error):
                result = {"ok": False, "error": "ValueError"} if error else {"ok": True}
                jid = self.start_job(self.reader, result, error=error)
                lock = CompletionLock(self.app.jobs._lock)
                subscribe = self.app.bus.subscribe

                def at_registration(*args, **kwargs):
                    if after_registration:
                        q = subscribe(*args, **kwargs)
                    self.complete_at_boundary(jid, lock)
                    if not after_registration:
                        q = subscribe(*args, **kwargs)
                    return q

                with mock.patch.object(self.app.jobs, "_lock", lock), \
                        mock.patch.object(self.app.bus, "subscribe", side_effect=at_registration):
                    q = self.subscribe(self.read_token, "/api/v1/jobs/" + jid + "/events")
                    self.finish_job(jid)
                self.assertEqual(q.get_nowait(), {
                    "event": "state", "job_id": jid, "status": "running", "result": None,
                })
                self.assertEqual(q.get_nowait(), {
                    "event": "error" if error else "done", "job_id": jid, "result": result,
                })
                self.assert_empty(q)

    def test_completion_before_queue_registration_is_not_lost(self):
        self.check_subscription_boundary(after_registration=False)

    def test_completion_after_queue_registration_cannot_overtake_initial_state(self):
        self.check_subscription_boundary(after_registration=True)

    def test_already_completed_job_starts_with_terminal_state_only(self):
        for error in (False, True):
            with self.subTest(error=error):
                result = {"ok": False, "error": "ValueError"} if error else {"ok": True}
                jid = self.start_job(self.reader, result, error=error)
                self.finish_job(jid)
                q = self.subscribe(self.read_token, "/api/v1/jobs/" + jid + "/events")
                self.assertEqual(q.get_nowait(), {
                    "event": "state", "job_id": jid,
                    "status": "error" if error else "done", "result": result,
                })
                self.assert_empty(q)

    def test_owner_check_precedes_capacity_and_retry_gets_initial_state(self):
        jid = self.start_job(self.reader, {"ok": True})
        path = "/api/v1/jobs/" + jid + "/events"
        self.app.bus.max_subs = 1
        occupied = self.subscribe(self.read_token)
        self.assertEqual(self.request(self.admin_token, path),
                         (404, {"ok": False, "error": "not_found"}))
        self.assertEqual(self.request(self.read_token, path),
                         (503, {"ok": False, "error": "busy"}))
        self.app.bus.unsubscribe(occupied)
        with mock.patch.object(self.app.jobs, "bus", None):
            self.assertEqual(self.request(self.read_token, path),
                             (503, {"ok": False, "error": "busy"}))
        q = self.subscribe(self.read_token, path)
        self.assertEqual(q.get_nowait(), {
            "event": "state", "job_id": jid, "status": "running", "result": None,
        })
        self.finish_job(jid)
        self.assertEqual(q.get_nowait(), {
            "event": "done", "job_id": jid, "result": {"ok": True},
        })

    def test_get_snapshot_cannot_mix_running_status_with_completed_result(self):
        result = {"ok": True, "value": "completion fixture"}
        jid = self.start_job(self.reader, result)
        rec = self.app.jobs._jobs[jid]
        lock = CompletionLock(self.app.jobs._lock)
        complete = self.complete_at_boundary

        class RecordView:
            def get(self, key, default=None):
                return rec.get(key, default)

            def __getitem__(self, key):
                value = rec[key]
                if key == "status":
                    complete(jid, lock)
                return value

        with mock.patch.object(self.app.jobs, "_lock", lock), \
                mock.patch.dict(self.app.jobs._jobs, {jid: RecordView()}):
            snapshot = self.app.jobs.get(jid, self.reader["id"])
            self.finish_job(jid)
        self.assertEqual(snapshot, {"id": jid, "status": "running", "result": None})
        self.assertEqual(self.app.jobs.get(jid, self.reader["id"]), {
            "id": jid, "status": "done", "result": result,
        })


class EventBus(unittest.TestCase):
    def test_payload_cannot_override_trusted_owner_or_job_metadata(self):
        bus = Bus()
        reader = bus.subscribe("reader")
        admin = bus.subscribe("admin")
        specific = bus.subscribe("admin", "requested")
        forged = {"event": "done", "owner": "reader", "job_id": "requested",
                  "result": {"ok": True, "value": "private fixture"}}
        bus.publish(forged, owner="admin", job_id="other")
        self.assertEqual(admin.get_nowait(), forged)
        for q in (reader, specific):
            with self.assertRaises(queue.Empty):
                q.get_nowait()

    def test_incomplete_private_metadata_fails_closed(self):
        bus = Bus()
        queues = [bus.subscribe("reader"), bus.subscribe(None), bus.subscribe("reader", "job")]
        forged = {"event": "done", "owner": "reader", "job_id": "job", "result": "private"}
        for metadata in ({"job_id": "job"}, {"owner": "", "job_id": "job"},
                         {"owner": "reader"}, {"owner": "reader", "job_id": ""}):
            bus.publish(forged, **metadata)
        for q in queues:
            with self.assertRaises(queue.Empty):
                q.get_nowait()

    def test_full_queue_still_drops_new_event(self):
        bus = Bus()
        q = bus.subscribe("reader")
        for n in range(101):
            bus.publish({"event": "mqtt", "n": n})
        self.assertEqual(q.maxsize, 100)
        self.assertEqual([q.get_nowait()["n"] for _ in range(100)], list(range(100)))
        with self.assertRaises(queue.Empty):
            q.get_nowait()


@unittest.skipUnless(hasattr(socketserver, "UnixStreamServer"), "Unix socket server unavailable")
class EventTransport(EventFixture, unittest.TestCase):
    def test_unix_http_stream_isolated_and_disconnect_releases_subscription(self):
        from sa02m_agent_api.__main__ import Request, UnixHTTPServer

        server = UnixHTTPServer(os.path.join(self.tmp_path, "api.sock"), Request)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop_server)
        disconnected = threading.Event()
        unsubscribe = self.app.bus.unsubscribe

        def observed_unsubscribe(q):
            unsubscribe(q)
            disconnected.set()

        observer = mock.patch.object(self.app.bus, "unsubscribe", side_effect=observed_unsubscribe)
        observer.start()
        self.addCleanup(observer.stop)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(server.server_address)
        stream = sock.makefile("rb")

        def disconnect():
            if sock.fileno() < 0:
                return
            sock.shutdown(socket.SHUT_RDWR)
            stream.close()
            sock.close()
            # Wake the SSE writer so its real finally path frees capacity.
            self.app.bus.publish({"event": "mqtt", "value": "disconnect fixture"})
            self.assertTrue(disconnected.wait(5), "SSE writer did not unsubscribe")

        self.addCleanup(disconnect)
        sock.sendall(("GET /api/v1/events HTTP/1.1\r\nX-SA02M-Token: %s\r\n\r\n"
                      % self.read_token).encode("ascii"))
        self.assertEqual(stream.readline(), b"HTTP/1.1 200 OK\r\n")
        headers = []
        while True:
            line = stream.readline()
            self.assertTrue(line, "HTTP headers ended early")
            if line == b"\r\n":
                break
            headers.append(line)
        self.assertIn(b"Content-Type: text/event-stream\r\n", headers)
        self.assertEqual(stream.readline(), b": connected\n")
        self.assertEqual(stream.readline(), b"\n")

        def read_event():
            line = stream.readline()
            self.assertTrue(line.startswith(b"data: "), line)
            self.assertEqual(stream.readline(), b"\n")
            return json.loads(line[6:])

        foreign = self.start_job(self.admin, {"ok": True, "value": "ADMIN-ONLY-SENTINEL"})
        self.finish_job(foreign)
        public = {"event": "mqtt", "value": "public barrier"}
        self.app.bus.publish(public)
        self.assertEqual(read_event(), public)
        own = self.start_job(self.reader, {"ok": True, "value": "owner fixture"})
        self.finish_job(own)
        self.assertEqual(read_event(), {"event": "start", "job_id": own})
        self.assertEqual(read_event(), {"event": "done", "job_id": own,
                                       "result": {"ok": True, "value": "owner fixture"}})
        disconnect()
        # A one-slot bus exposes a leaked connection without inspecting its internals.
        self.app.bus.max_subs = 1
        self.subscribe(self.read_token)


if __name__ == "__main__":
    unittest.main()
