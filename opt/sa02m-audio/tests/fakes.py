"""Test doubles: an in-memory paho client, a fake `arecord` on PATH, and a
board-like sandbox (conf, eMMC store, /proc/asound, /proc/mounts, a removable
mount point) for the daemon."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import types
from typing import Dict, List, Optional, Tuple

from sa02m_audio import constants as C

# /proc/asound/cards on bench 1.135 (2026-10-15), verbatim.
BENCH_CARDS = (
    " 0 [Codec          ]: H3_Audio_Codec - H3 Audio Codec\n"
    "                      H3 Audio Codec\n"
    " 1 [Microphone     ]: USB-Audio - Fifine Microphone\n"
    "                      Fifine Microphone at usb-1c19400.usb-1, full speed\n"
)
ROOT_MOUNT = "/dev/root / ext4 rw,noatime,errors=remount-ro,commit=600 0 0\n"


class FakePaho:
    """Just enough of paho.mqtt.client.Client (1.x callbacks) for mqttio.Link."""

    def __init__(self) -> None:
        self.published: List[Tuple[str, str, bool]] = []
        self.subscribed: List[str] = []
        self.will: Optional[Tuple[str, str, int, bool]] = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.lock = threading.Lock()

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass

    def connect_async(self, host, port=1883, keepalive=60):
        self.address = (host, port)

    def loop_start(self):
        self.on_connect(self, None, {}, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        with self.lock:
            self.published.append((topic, "" if payload is None else str(payload), bool(retain)))
        return types.SimpleNamespace(rc=0)

    def deliver(self, topic: str, payload: str, retain: bool = False) -> None:
        self.on_message(self, None, types.SimpleNamespace(
            topic=topic, payload=payload.encode("utf-8"), retain=retain))

    def snapshot(self) -> List[Tuple[str, str, bool]]:
        with self.lock:
            return list(self.published)

    def retained(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for topic, payload, retain in self.snapshot():
            if retain:
                out[topic] = payload
        return out

    def values(self, control: str) -> List[str]:
        topic = "%s/controls/%s" % (C.DEVICE_TOPIC, control)
        return [p for t, p, _r in self.snapshot() if t == topic]


FAKE_ARECORD = r'''#!{python}
"""Fake arecord: raw S16_LE mono PCM on stdout. Env FAKE_ARECORD_MODE:
tone (default, ~-6 dBFS square), silence, busy, nodev, die (64000 bytes then
exit 1). FAKE_ARECORD_REALTIME=1 paces output at 32000 B/s."""
import os, sys, time
mode = os.environ.get("FAKE_ARECORD_MODE", "tone")
log = os.environ.get("FAKE_ARECORD_ARGV")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
if mode == "busy":
    sys.stderr.write("arecord: main:850: audio open error: Device or resource busy\n")
    sys.exit(1)
if mode == "nodev":
    sys.stderr.write("arecord: main:850: audio open error: No such file or directory\n")
    sys.exit(1)
if mode == "xrun":
    sys.stderr.write("overrun!!! (at least 1.234 ms long)\n")
    sys.stderr.flush()
sample = b"\x00\x00" if mode == "silence" else (16384).to_bytes(2, "little", signed=True)
other = b"\x00\x00" if mode == "silence" else (-16384).to_bytes(2, "little", signed=True)
chunk = (sample * 8 + other * 8) * 100  # 3200 bytes = 0.1 s
realtime = os.environ.get("FAKE_ARECORD_REALTIME") == "1"
sent = 0
out = sys.stdout.buffer
try:
    while True:
        out.write(chunk)
        out.flush()
        sent += len(chunk)
        if mode == "die" and sent >= 64000:
            sys.exit(1)
        time.sleep(0.1 if realtime else 0.001)
except (BrokenPipeError, OSError):
    sys.exit(0)
'''


def fake_arecord_dir(root: str) -> str:
    """A dir holding an executable `arecord` fake; prepend it to PATH."""
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    path = os.path.join(bindir, C.ARECORD)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(FAKE_ARECORD.replace("{python}", sys.executable))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


class Sandbox:
    """A board-shaped tree under one temp dir. The removable medium `usb` is
    `<root>/media/usb`; `mount_usb()` lists it in the fake /proc/mounts and
    gives it its own st_dev through `dev_of` (a test cannot mount)."""

    FAKE_DEV = 0x7E57

    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sa02m-audio-")
        self.root = self._td.name
        p = lambda *a: os.path.join(self.root, *a)  # noqa: E731
        self.var_dir = p("var")
        self.run_dir = p("run")
        self.etc_dir = p("etc")
        self.asound = p("asound")
        self.usb = p("media", "usb")
        self.sdcard = p("media", "sdcard")
        for d in (self.var_dir, self.run_dir, self.etc_dir, self.asound, self.usb, self.sdcard):
            os.makedirs(d)
        self.conf = os.path.join(self.etc_dir, "sa02m-audio.conf")
        self.mounts = p("mounts")
        self.mic_lock = p("sa02m-mic.lock")
        self.status = os.path.join(self.run_dir, "status.json")
        self.events = os.path.join(self.var_dir, "events.jsonl")
        self.lock = os.path.join(self.run_dir, "daemon.lock")
        self.media = (
            C.Medium(C.MEDIUM_EMMC, None, self.var_dir, 1, 256),
            C.Medium(C.MEDIUM_USB, self.usb, os.path.join(self.usb, C.REMOVABLE_STORE_NAME), 1, 4096),
            C.Medium(C.MEDIUM_SDCARD, self.sdcard,
                     os.path.join(self.sdcard, C.REMOVABLE_STORE_NAME), 1, 4096),
        )
        self.mounted: Dict[str, str] = {}
        self.write_mounts()
        self.set_cards(BENCH_CARDS, capture=(1,))

    def cleanup(self) -> None:
        self._td.cleanup()

    def set_cards(self, text: str, capture=()) -> None:
        with open(os.path.join(self.asound, "cards"), "w", encoding="utf-8") as fh:
            fh.write(text)
        for name in os.listdir(self.asound):
            if name.startswith("card") and name[4:].isdigit():
                for sub in os.listdir(os.path.join(self.asound, name)):
                    os.rmdir(os.path.join(self.asound, name, sub))
                os.rmdir(os.path.join(self.asound, name))
        for index in capture:
            os.makedirs(os.path.join(self.asound, "card%d" % index, "pcm0c"))

    def write_mounts(self, extra: str = "") -> None:
        lines = ROOT_MOUNT
        for mount_point, line in self.mounted.items():
            lines += line
        with open(self.mounts, "w", encoding="utf-8") as fh:
            fh.write(lines + extra)

    def mount(self, mount_point: str, source: str = "/dev/sda1", fstype: str = "vfat",
              options: str = "rw,relatime,umask=000") -> None:
        self.mounted[mount_point] = "%s %s %s %s 0 0\n" % (
            source, mount_point.replace(" ", "\\040"), fstype, options)
        self.write_mounts()

    def unmount(self, mount_point: str) -> None:
        self.mounted.pop(mount_point, None)
        self.write_mounts()

    def dev_of(self, path: str) -> int:
        if path in self.mounted:
            return self.FAKE_DEV
        return os.stat(path).st_dev

    def write_conf(self, **values) -> None:
        body = {"enabled": "true"}
        body.update({k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in values.items()})
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write("[recorder]\n" + "".join("%s = %s\n" % kv for kv in body.items()))

    def rec(self, medium: str = C.MEDIUM_EMMC) -> str:
        base = {C.MEDIUM_EMMC: self.var_dir, C.MEDIUM_USB: os.path.join(self.usb, "sa02m-audio"),
                C.MEDIUM_SDCARD: os.path.join(self.sdcard, "sa02m-audio")}[medium]
        return os.path.join(base, C.REC_SUBDIR)

    def tmp(self, medium: str = C.MEDIUM_EMMC) -> str:
        return os.path.join(os.path.dirname(self.rec(medium)), C.TMP_SUBDIR)

    def clips(self, medium: str = C.MEDIUM_EMMC) -> List[str]:
        try:
            return sorted(os.listdir(self.rec(medium)))
        except FileNotFoundError:
            return []


def wait_for(predicate, timeout: float = 10.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


class DaemonHarness:
    """The real service.Daemon loop on a Sandbox, with mqttio.Link over a
    FakePaho and the fake arecord first on PATH. `start()` runs it in a
    thread; `stop()` sets its stop event and joins."""

    def __init__(self, sandbox: Sandbox, *, mode: str = "tone", realtime: bool = False,
                 **daemon_kwargs) -> None:
        from sa02m_audio import mqttio, service

        self.sb = sandbox
        self.paho = FakePaho()
        self.stop_event = threading.Event()
        self.bindir = fake_arecord_dir(sandbox.root)
        self.argv_log = os.path.join(sandbox.root, "arecord.argv")
        self._env = {"PATH": self.bindir + os.pathsep + os.environ.get("PATH", ""),
                     "FAKE_ARECORD_MODE": mode, "FAKE_ARECORD_REALTIME": "1" if realtime else "0",
                     "FAKE_ARECORD_ARGV": self.argv_log}
        self._saved: Dict[str, Optional[str]] = {}
        kwargs = dict(stop=self.stop_event, conf_path=sandbox.conf, media=sandbox.media,
                      mounts_file=sandbox.mounts, root=sandbox.root, dev_of=sandbox.dev_of,
                      asound_dir=sandbox.asound, mic_lock=sandbox.mic_lock,
                      status_file=sandbox.status, events_file=sandbox.events,
                      daemon_lock=sandbox.lock,
                      link_factory=lambda inbox: mqttio.Link(inbox, client_factory=lambda: self.paho),
                      check_dependencies=lambda: [])
        kwargs.update(daemon_kwargs)
        self.daemon = service.Daemon(**kwargs)
        self.exit_code: Optional[int] = None
        self.thread: Optional[threading.Thread] = None

    def set_mode(self, mode: str) -> None:
        os.environ["FAKE_ARECORD_MODE"] = mode

    def start(self, wait_ready: bool = True) -> "DaemonHarness":
        for key, value in self._env.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

        def _run():
            self.exit_code = self.daemon.run()

        self.thread = threading.Thread(target=_run, name="audio-daemon", daemon=True)
        self.thread.start()
        if wait_ready:
            assert wait_for(lambda: self.paho.subscribed or self.exit_code is not None), "daemon never connected"
        return self

    def stop(self) -> Optional[int]:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(20)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self.exit_code

    def command(self, control: str, payload: str, retain: bool = False) -> None:
        self.paho.deliver("%s/controls/%s/on" % (C.DEVICE_TOPIC, control), payload, retain)

    def last(self, control: str) -> Optional[str]:
        values = self.paho.values(control)
        return values[-1] if values else None

    def wait_last(self, control: str, value: str, timeout: float = 10.0) -> bool:
        return wait_for(lambda: self.last(control) == value, timeout)

    def wait_idle_after_record(self, timeout: float = 15.0) -> bool:
        """record went 1 and is back to 0."""
        def done():
            vals = self.paho.values("record")
            return "1" in vals and vals[-1] == "0"
        return wait_for(done, timeout)

    def events(self) -> list:
        import json
        try:
            with open(self.sb.events, encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []

    def status(self) -> dict:
        import json
        with open(self.sb.status, encoding="utf-8") as fh:
            return json.load(fh)
