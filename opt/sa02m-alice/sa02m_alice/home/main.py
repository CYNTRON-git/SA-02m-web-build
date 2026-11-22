"""Standalone board service: LAN stays available while cloud control is off."""
from __future__ import annotations

import fcntl
import grp
import logging
import os
import signal
import stat
import threading
from pathlib import Path

from ..client.device_registry import DeviceRegistry
from ..client.mqtt_link import MqttLink, MqttUnavailable
from ..client.reload_watch import DevicesWatcher, RetainedGrace, apply_reload
from ..common import constants as C
from ..common.config_store import controller_sn
from .adapter import BoardAdapter
from .bridge import SOCKET_PATH
from .contract import identifier
from .dispatcher import Dispatcher
from .ledger import Ledger
from .publisher import publish_once
from .server import Server


def run():
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("sa02m-home")
    os.umask(0o077)
    state = Path("/var/lib/sa02m-home")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (state / "owner.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cid = identifier(controller_sn())
    if cid == "sa02m":
        raise RuntimeError("stable controller identity required")
    registry = DeviceRegistry(profile=C.PROFILE_CLOUD)
    ledger = Ledger(state / "receipts.sqlite3")
    dispatcher = Dispatcher(cid, registry, publish_once, ledger, BoardAdapter(registry))
    grace = RetainedGrace()
    stop = threading.Event()
    watcher = DevicesWatcher(C.DEVICES_CONF)

    def message(_client, _userdata, msg):
        dispatcher.note_mqtt(msg.topic, msg.payload.decode("utf-8", "replace"), retained=bool(msg.retain))

    # This API publishes to the board's private localhost broker only; no
    # request or configuration document can choose an external MQTT endpoint.
    mqtt = MqttLink("127.0.0.1", 1883, topics=dispatcher.topics,
                    grace=grace, grace_s=2, on_message=message, client_id="sa02m-home-v1")
    dispatcher.broker_available = lambda: mqtt.connected
    path = Path(SOCKET_PATH)
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    gid = grp.getgrnam("www-data").gr_gid
    os.chown(path.parent, 0, gid)
    os.chmod(path.parent, 0o750)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError("refusing unexpected socket path")
        path.unlink()  # Exclusive stable owner.lock above excludes another owner.
    server = Server(str(path), dispatcher)
    os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    connected_once = False
    subscribed = set()
    try:
        while not stop.wait(1):
            if not connected_once:
                try:
                    mqtt.connect()
                    mqtt.subscribe_all()
                    subscribed = dispatcher.topics()
                    connected_once = True
                except (OSError, MqttUnavailable):
                    mqtt.close()
                    stop.wait(4)
            with dispatcher.lock:
                if watcher.changed():
                    apply_reload(registry, mqtt, grace, window_s=2, log=log)
                valid = dispatcher.topics()
                if valid != subscribed and connected_once:
                    for topic in subscribed - valid:
                        mqtt.unsubscribe(topic)
                    mqtt.subscribe_all()
                    subscribed = valid
                    dispatcher.observations = {k: v for k, v in dispatcher.observations.items() if k in valid}
                dispatcher._expire_pending()
    finally:
        server.shutdown()
        server.server_close()
        mqtt.close()
        ledger.close()
        path.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    run()
