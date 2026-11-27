"""Dashboard roster from the saved bridge config and the active bridge invocation."""

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from roster_files import CONFIG_LIMIT, CONFIG_PATH, ROSTER_LIMIT, fresh, read_regular, regular_revision


BRIDGE = Path(__file__).resolve().parents[1] / "sa02m-modbus-mqtt"
MEMO = os.environ.get("SA02M_RS485_ROSTER_MEMO", "/run/sa02m-rs485-polling.memo")
_BINDING = re.compile(r"[0-9a-f]{64}")
_WORKER_SCHEMA = 2


def _inputs():
    web_root = Path("/var/www") if BRIDGE.parent == Path("/opt") else BRIDGE.parents[1] / "www"
    try:
        release = regular_revision(web_root / "network_config/VERSION")
    except FileNotFoundError:
        release = None
    # The old installer may copy a driver after the web VERSION or the facade.
    # Track the shipped admission code, including its family constructors.
    sources = [BRIDGE / "modbus_mqtt_bridge.py", *BRIDGE.glob("devices_tables.py"), *BRIDGE.glob("bridge_*.py")]
    for family in ("carel", "led", "spodes"):
        package = BRIDGE.parent / ("sa02m-" + family) / ("sa02m_" + family)
        sources.extend(package.glob("*.py"))
    return [release, regular_revision(__file__),
            [[str(p.relative_to(BRIDGE.parent)), regular_revision(p)] for p in sorted(sources)]]


# A process which overlaps module replacement must leave the next timer to rebuild.
try:
    _LOADED_INPUTS = _inputs()
except (OSError, ValueError):
    _LOADED_INPUTS = None


def _aliases():
    return [os.path.realpath("/dev/COM%d" % n) for n in range(1, 6)]


def _source_rows(data, aliases):
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if not hasattr(self, "node_sizes"):
                self.node_sizes = {}
            alias = self.check_event(yaml.AliasEvent)
            node = super().compose_node(parent, index)
            if alias and node not in self.node_sizes:
                raise ValueError("recursive YAML alias")
            if node not in self.node_sizes:
                if isinstance(node, yaml.MappingNode):
                    children = (child for pair in node.value for child in pair)
                elif isinstance(node, yaml.SequenceNode):
                    children = iter(node.value)
                else:
                    children = ()
                # Bound alias expansion before SafeLoader flattens merged mappings.
                size = 1 + sum(self.node_sizes.get(child, ROSTER_LIMIT) for child in children)
                if size > CONFIG_LIMIT:
                    raise ValueError("excessive YAML expansion")
                self.node_sizes[node] = size
            return node

        def construct_mapping(self, node, deep=False):
            keys = set()
            for key, _ in node.value:
                if key.tag == "tag:yaml.org,2002:merge":
                    continue
                name = self.construct_object(key)
                if name in keys:
                    raise ValueError("duplicate mapping key")
                keys.add(name)
            return super().construct_mapping(node, deep)

    try:
        cfg = yaml.load(data, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ValueError("invalid document") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("devices"), list) or "Devices" in cfg:
        raise ValueError("missing device list")
    # Constructors normally describe bad config in their bridge log; this worker
    # reads a credential-bearing document and reports only a fixed diagnostic.
    previous = logging.root.manager.disable
    import_path = sys.path[:]
    logging.disable(logging.CRITICAL)
    try:
        sys.path.insert(0, str(BRIDGE))
        import bridge_roster
        projected = bridge_roster.prepare(cfg["devices"], {target: "COM%d" % n
                                                          for n, target in enumerate(aliases, 1)})
    finally:
        sys.path[:] = import_path
        logging.disable(previous)
    return [{k: row[k] for k in ("com", "addr", "model", "ours", "binding")}
            for row in projected if row["com"]]


def _valid_rows(rows):
    return isinstance(rows, list) and all(
        isinstance(r, dict) and set(r) == {"com", "addr", "model", "ours", "binding"}
        and isinstance(r["com"], str) and re.fullmatch(r"COM[1-5]", r["com"])
        and type(r["addr"]) is int and type(r["ours"]) is bool
        and isinstance(r["model"], str) and len(r["model"]) <= 64
        and (r["binding"] is None or isinstance(r["binding"], str) and _BINDING.fullmatch(r["binding"]))
        for r in rows)


def _write_memo(path, key, rows):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".rs485-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"key": key, "rows": rows}, out, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def configured_rows(path, memo):
    source = regular_revision(path)
    inputs = _inputs()
    if inputs != _LOADED_INPUTS:
        raise ValueError("producer changed")
    aliases = _aliases()
    key = [_WORKER_SCHEMA, source, inputs, aliases]
    try:
        saved = json.loads(read_regular(memo, ROSTER_LIMIT, private=True)[0])
        if isinstance(saved, dict) and saved.get("key") == key and _valid_rows(saved.get("rows")):
            return source, saved["rows"]
    except (OSError, ValueError, RecursionError):
        pass
    data, actual = read_regular(path)
    if actual != source:
        raise ValueError("config changed")
    rows = _source_rows(data, aliases)
    if regular_revision(path) != source or _inputs() != inputs or _aliases() != aliases:
        raise ValueError("projection input changed")
    _write_memo(memo, key, rows)
    return source, rows


def active_invocation():
    try:
        res = subprocess.run(["systemctl", "show", "sa02m-modbus-mqtt.service",
                              "--property=ActiveState,InvocationID"], capture_output=True,
                             text=True, check=True, timeout=1)
        state = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        return state.get("InvocationID") if state.get("ActiveState") == "active" else None
    except (OSError, subprocess.SubprocessError):
        return None


def polling_ports(rows, runtime, now, owner=active_invocation):
    states = {}
    instance = runtime.get("invocation") if isinstance(runtime, dict) else None
    if (rows and isinstance(instance, str) and re.fullmatch(r"[0-9a-f]{32}", instance)
            and instance != "0" * 32 and fresh(runtime.get("ts"), now)
            and isinstance(runtime.get("devices"), list) and owner() == instance):
        for item in runtime["devices"]:
            if not isinstance(item, dict):
                continue
            binding = item.get("binding")
            if isinstance(binding, str) and _BINDING.fullmatch(binding):
                state = item.get("observed_online")
                stamp = item.get("observed_at")
                states[binding] = ((state, stamp) if type(state) is bool and binding not in states
                                   and fresh(stamp, now) else None)
    ports = {}
    for row in rows:
        observation = states.get(row["binding"])
        state, stamp = observation or (None, None)
        port = ports.setdefault(row["com"], {"source": "configured", "live": False, "ours": [],
                                             "third_party": {"total": 0, "online": 0}})
        if state is not None:
            port["live"] = True
            port["ts"] = max(port.get("ts", 0), stamp)
        if row["ours"]:
            port["ours"].append({"addr": row["addr"], "model": row["model"],
                                 "online": state, "observed_at": stamp, "source": "configured"})
        else:
            third = port["third_party"]
            third["total"] += 1
            if stamp is not None:
                port["third_party_latest_at"] = max(port.get("third_party_latest_at", 0), stamp)
            if state is None:
                third["online"] = None
            elif third["online"] is not None:
                third["online"] += int(state)
                port["third_party_observed_at"] = min(port.get("third_party_observed_at", stamp), stamp)
    for port in ports.values():
        port["ours"].sort(key=lambda row: row["addr"])
        if not port["third_party"]["total"]:
            port["third_party"]["online"] = None
    return ports


def projection(runtime, now, path=CONFIG_PATH, memo=MEMO, owner=active_invocation):
    try:
        rev, rows = configured_rows(path, memo)
        ports = polling_ports(rows, runtime, now, owner)
        if rev != regular_revision(path):
            raise ValueError("config changed")
        return {"config_revision": rev, "polling_ports": ports}
    except (ImportError, OSError, ValueError, TypeError, RecursionError, OverflowError):
        print("rs485-roster: configured membership unavailable", file=sys.stderr)
        return {"polling_ports": {}}
