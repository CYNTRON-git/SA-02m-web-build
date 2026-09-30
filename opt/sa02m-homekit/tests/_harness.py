"""A Daemon wired to fakes (runner, MQTT, document watcher) in a sandbox —
shared by test_daemon.py and test_no_code_leak.py. No pyhap, no network."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from sa02m_alice.config import scene_devices

from sa02m_homekit import config
from sa02m_homekit import constants as C
from sa02m_homekit import netif
from sa02m_homekit.main import Daemon
from sa02m_homekit.status import StatusWriter

FAST = {
    "FLUSH_S": 0.01,
    "DOC_POLL_S": 0.02,
    "ADDR_POLL_S": 0.03,
    "STATUS_HEARTBEAT_S": 0.05,
    "REBUILD_DEBOUNCE_S": 0.05,
}

DOC = {
    "rooms": [], "groups": [],
    "devices": [
        {"id": "relay", "name": "Реле", "type": "devices.types.socket", "homekit_visible": True,
         "capabilities": [{"type": "devices.capabilities.on_off",
                           "mqtt": "/devices/mr02m-COM3-10/controls/do_1",
                           "parameters": {"instance": "on"}}], "properties": []},
    ],
}


class FakeRunner:
    def __init__(self, harness: "Harness", **kw: Any) -> None:
        self.h = harness
        self.kw = kw
        self.running = False
        self.paired = False
        self.pairings = 0
        self.pincode = kw["new_code"]
        self.setup_id = kw["new_setup_id"]
        self.pair_setup_locked = False
        self.pair_setup_failures = kw["pair_setup_failures"]
        self.pushed: List[Any] = []
        self.events: List[Any] = []
        self.stopped = False

    def start(self) -> None:
        if self.h.start_error is not None:
            raise self.h.start_error
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.stopped = True

    def push(self, device_id: str, values: Any, available: bool) -> None:
        self.pushed.append((device_id, values, available))

    def push_event(self, device_id: str, index: int, char: str, value: Any) -> None:
        self.events.append((device_id, index, char, value))

    def pair(self) -> None:
        self.paired = True
        self.pairings = 1
        self.kw["on_pairing_changed"]()


class FakeMqtt:
    def __init__(self, on_message: Callable[..., None], topics: Any) -> None:
        self.on_message = on_message
        self.topics = set(topics)
        self.started = self.stopped = False
        self.published: List[Any] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set_topics(self, topics: Any) -> None:
        self.topics = set(topics)

    def publish_command(self, topic: str, payload: str) -> bool:
        self.published.append((topic, payload))
        return True


class FakeWatcher:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness

    def changed(self) -> bool:
        flag, self.h.doc_changed = self.h.doc_changed, False
        return flag


class FakeRulesWatcher:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness

    def changed(self) -> bool:
        flag, self.h.rules_changed = self.h.rules_changed, False
        return flag


class Harness:
    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp()
        d = self.dir
        self.var = os.path.join(d, "var")
        self.run_dir = os.path.join(d, "run")
        os.makedirs(self.var)
        os.makedirs(self.run_dir)
        self.conf_path = os.path.join(d, "sa02m-homekit.conf")
        self.machine_id = os.path.join(d, "machine-id")
        with open(self.machine_id, "w") as fh:
            fh.write("0123456789abcdef\n")
        self.doc = copy.deepcopy(DOC)
        self.doc_changed = False
        # The scenario store as the scene reader sees it (never the host's
        # /etc/sa02m-rules): `rules_readable = False` is what `read_rules_doc`
        # answers for a torn store — proven on the real store by
        # test_engine.SceneRealStoreTests.
        self.rules: Dict[str, Any] = {"scenarios": []}
        self.rules_readable = True
        self.rules_changed = False
        self.address: Optional[str] = "192.168.1.136"
        self.start_error: Optional[BaseException] = None
        self.missing: List[str] = []
        self.runners: List[FakeRunner] = []
        self.mqtt: Optional[FakeMqtt] = None
        self.stop = threading.Event()
        self.result: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        patches: Dict[str, Any] = dict(FAST)
        patches.update({
            "VAR_DIR": self.var,
            "STATE_FILE": os.path.join(self.var, "state.json"),
            "AIDS_FILE": os.path.join(self.var, "aids.json"),
            "IDENTITY_FILE": os.path.join(self.var, "identity.json"),
            "MACHINE_ID_FILE": self.machine_id,
            "VERSION_FILE": os.path.join(d, "VERSION"),
        })
        self._patches = [mock.patch.object(C, k, v) for k, v in patches.items()]
        self._patches += [
            mock.patch.object(scene_devices, "load_rules_doc",
                              lambda *_a, **_k: copy.deepcopy(self.rules) if self.rules_readable else {}),
            mock.patch.object(scene_devices, "read_rules_doc",
                              lambda *_a, **_k: ((copy.deepcopy(self.rules), True)
                                                 if self.rules_readable else ({}, False))),
        ]
        for p in self._patches:
            p.start()
        self.status = StatusWriter(self.run_dir)
        self.daemon = Daemon(
            stop=self.stop,
            status=self.status,
            conf_path=self.conf_path,
            devices_path=os.path.join(d, "devices.conf"),
            runner_factory=self._runner,
            mqtt_factory=self._mqtt,
            load_document=lambda _p: copy.deepcopy(self.doc),
            watcher_factory=lambda _p: FakeWatcher(self),
            rules_watcher_factory=lambda: FakeRulesWatcher(self),
            address_of=lambda _n: self.address,
            mac_of=lambda _n: "02:42:ac:11:00:02",
            check_dependencies=lambda: list(self.missing),
        )

    # factories
    def _runner(self, **kw: Any) -> FakeRunner:
        runner = FakeRunner(self, **kw)
        self.runners.append(runner)
        return runner

    def _mqtt(self, on_message: Callable[..., None], topics: Any) -> FakeMqtt:
        self.mqtt = FakeMqtt(on_message, topics)
        return self.mqtt

    # control
    def conf(self, **kw: Any) -> None:
        config.save(config.BridgeConfig(**kw), self.conf_path)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.result = self.daemon.run()

    def join(self, timeout: float = 5.0) -> Optional[int]:
        assert self._thread is not None
        self._thread.join(timeout)
        return self.result

    def finish(self) -> Optional[int]:
        self.stop.set()
        return self.join()

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except (OSError, ValueError, KeyError, IndexError):
                pass
            time.sleep(0.01)
        return False

    # observations
    def status_json(self) -> Dict[str, Any]:
        with open(self.status.status_path, encoding="utf-8") as fh:
            return json.load(fh)

    def setup_exists(self) -> bool:
        return os.path.exists(self.status.setup_path)

    def setup_json(self) -> Dict[str, Any]:
        with open(self.status.setup_path, encoding="utf-8") as fh:
            return json.load(fh)

    def state(self) -> str:
        return self.status_json()["state"]

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self.finish()
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


__all__ = ["Harness", "netif"]
