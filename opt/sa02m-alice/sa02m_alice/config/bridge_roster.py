"""The bridge family roster used by Alice, HomeKit and mixed-version Rules."""

from functools import lru_cache
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


MAX_BYTES = 1048576  # The MQTT config helper's input limit.


class AmbiguousDocument(dict):
    """PyYAML loaded the producer's effective rows from a duplicate-key document."""


class DuplicateKey(ValueError):
    pass


def read_regular(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise ValueError("invalid file")
        data = stream.read(MAX_BYTES + 1)
        after = os.stat(path, follow_symlinks=False)
        if (len(data) > MAX_BYTES or not os.path.samestat(before, after)
                or before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size
                or (os.name != "nt" and before.st_ctime_ns != after.st_ctime_ns)):
            raise ValueError("file changed during read")
        return data, before


def parse_yaml(content):
    import yaml

    class RosterLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            # PyYAML silently overwrites repeated keys. Check explicit entries
            # before it applies YAML's normal merge/override semantics.
            if isinstance(node, yaml.MappingNode):
                keys = set()
                for key_node, _ in node.value:
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        continue
                    key = self.construct_object(key_node)
                    try:
                        if key in keys:
                            raise DuplicateKey("duplicate YAML mapping key")
                        keys.add(key)
                    except TypeError:
                        raise ValueError("invalid YAML mapping key") from None
            return super().construct_mapping(node, deep)

    try:
        return yaml.load(content, Loader=RosterLoader)
    except DuplicateKey:
        doc = yaml.safe_load(content)
        return AmbiguousDocument(doc) if isinstance(doc, dict) else None


def load_yaml(path):
    try:
        import yaml
    except ImportError:
        return None
    try:
        return parse_yaml(read_regular(path)[0])
    except (OSError, ValueError, RecursionError, yaml.YAMLError):
        return None


def yaml_devices(doc):
    """The picker also supports legacy id mappings and rows with explicit controls."""
    if type(doc) is not dict or ("devices" in doc) == ("Devices" in doc):
        return None
    devices = doc["devices"] if "devices" in doc else doc["Devices"]
    if isinstance(devices, list):
        return devices if all(isinstance(row, dict) for row in devices) else None
    if not isinstance(devices, dict):
        return None
    rows = []
    for key, row in devices.items():
        if (type(key) not in (str, int) or not str(key).strip() or not isinstance(row, dict)
                or ("id" in row and row["id"] != key)):
            return None
        rows.append(dict(row, id=key))
    return rows


def picker_id(row):
    """Keep an explicit MQTT id byte-for-byte, including spaces within its topic."""
    name = row.get("id", row.get("name"))
    return str(name) if type(name) in (str, int) and str(name).strip() else ""


_HERE = Path(__file__).resolve()
_OPT = _HERE.parents[2 if _HERE.parent.name == "sa02m_rules" else 3]
PRODUCER = _OPT / "sa02m-modbus-mqtt" / "modbus_mqtt_bridge.py"
# The released .32 compose routine only registers in memory. Unknown older
# entry points have no declared bus-free capability and cannot clear an alarm.
_RELEASE_32 = "618f8a6ff15c688faa04e4c70a3049cff82cdfbe3e83096a4c3c2b62b036c73a"


def _legacy_registration(entry, rows):
    class Registry:
        def register_device(self, name):
            hash(name)  # The released MQTT register stores this exact key.

    doc = {"devices": deepcopy(rows)}
    entry["_migrate_config_ai_sensor_types"](doc)
    buses, _, refused = entry["compose_pollers"](doc["devices"], Registry())
    accepted = [("led" if isinstance(poller, entry["LedPoller"]) else "other", poller.device_id)
                for pollers in buses.values() for poller in pollers]
    # .32 silently skipped unknown types. A skipped row cannot attest to a
    # replacement, even when another row with that id registered successfully.
    rejected = {record["index"] for record in refused}
    for index, row in enumerate(rows):
        if index not in rejected and (row.get("type", "").lower() not in entry["POLLER_CLASSES"]
                                      or type(row.get("id")) not in (str, int) or not str(row["id"]).strip()):
            refused.append({"index": index, "id": row.get("id"), "reason": "invalid_poller_config"})
    return accepted, refused


@lru_cache(maxsize=2)
def _entry_code(path, revision):
    content, _ = read_regular(path)
    legacy = sha256(content.replace(b"\r\n", b"\n")).hexdigest() == _RELEASE_32
    if not legacy and b"\ndef prepare_poller_registration(" not in content:
        raise ImportError("producer has no bus-free registration capability")
    return compile(content, str(path), "exec"), legacy


def _producer_code():
    st = PRODUCER.stat()
    return _entry_code(PRODUCER, (st.st_ino, st.st_ctime_ns, st.st_mtime_ns, st.st_size))


def _producer_namespace(code):
    directory = str(PRODUCER.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    entry = {"__name__": "_sa02m_family_producer", "__file__": str(PRODUCER)}
    exec(code, entry)
    return entry


def bridge_admission(rows):
    code, legacy = _producer_code()
    if legacy:
        import yaml
        # .32 mutates module globals and logs constructor inputs. A private
        # interpreter contains both, with credentials confined to its stdin.
        flags = ["-I"] if sys.flags.isolated else ["-E"]
        result = subprocess.run([sys.executable, *flags, "-B", str(_HERE), "--release-32"],
                                input=yaml.safe_dump(rows).encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=True, timeout=7)
        return json.loads(result.stdout)
    prepared, refused = _producer_namespace(code)["prepare_poller_registration"](rows)
    return [(kind, poller.device_id) for kind, poller, _ in prepared.values()], refused


class FamilyEvidence(dict):
    """None keeps the last family for that id; an anonymous refusal keeps unmentioned ids."""
    def __init__(self, values=(), *, unidentified=False):
        super().__init__(values)
        self.unidentified = unidentified


def _possible_leds(rows, *, uncertain_ids=False):
    # With no trustworthy negative evidence, an explicit LED can still arm
    # protection, including on an alias previously owned by a template.
    result = FamilyEvidence(unidentified=True)
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) not in (str, int):
            continue
        name = str(row["id"])
        if not name.strip():
            continue
        if isinstance(row.get("type"), str) and row["type"].lower() == "led":
            result[name] = True
        elif uncertain_ids:
            result.setdefault(name, None)
    return result or None


def merge_families(previous, evidence):
    if evidence is None:
        return previous
    current = previous.copy() if getattr(evidence, "unidentified", False) else {}
    for name, led in evidence.items():
        current[name] = previous.get(name, True) if led is None else led
    return current


def admitted_families(doc):
    """Only the bridge's actual device list can replace a known family."""
    if not isinstance(doc, dict) or not isinstance(doc.get("devices"), list):
        return None
    rows = doc["devices"]
    if isinstance(doc, AmbiguousDocument) or "Devices" in doc:
        return _possible_leds(rows)
    if not rows:
        return FamilyEvidence()
    try:
        prepared, refused = bridge_admission(rows)
    except (AttributeError, ImportError, IndexError, KeyError, OSError, OverflowError, RecursionError,
            subprocess.SubprocessError, SystemExit, TypeError, ValueError):
        return _possible_leds(rows, uncertain_ids=True)
    # A missing reader dependency may remove a real producer participant
    # from a shared framing or endpoint constraint. No negative is proven.
    if any(entry["reason"] == "driver_unavailable" for entry in refused):
        return _possible_leds(rows, uncertain_ids=True)
    rejected = set()
    unidentified = False
    result = FamilyEvidence()
    for entry in refused:
        name = entry["id"]
        if type(name) not in (str, int) or not str(name).strip():
            unidentified = True
        else:
            rejected.add(str(name))
    for kind, device_id in prepared:
        name = str(device_id)
        if type(device_id) not in (str, int) or not name.strip():
            continue
        if kind == "led":
            result[name] = True
        elif not unidentified and name not in rejected:
            result.setdefault(name, False)
    if refused and not result:
        return None
    result.unidentified = unidentified
    for name in rejected:
        result.setdefault(name, None)
    return result


if __name__ == "__main__" and sys.argv[1:] == ["--release-32"]:
    import yaml
    code, legacy = _producer_code()
    if not legacy:
        raise SystemExit(1)
    rows = yaml.safe_load(sys.stdin.buffer.read())
    accepted, refused = _legacy_registration(_producer_namespace(code), rows)
    sys.stdout.buffer.write(json.dumps([accepted, refused]).encode())
