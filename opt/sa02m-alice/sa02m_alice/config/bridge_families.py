"""Bridge device families for consumers that cannot read the broker credentials."""

from __future__ import annotations

import json
import os
import stat
import tempfile

from ..common.config_store import file_fingerprint, file_revision
from .bridge_roster import PRODUCER, FamilyEvidence, MAX_BYTES, admitted_families, merge_families, parse_yaml, read_regular, yaml_devices

SOURCE = "/etc/sa02m-modbus-mqtt.yaml"
INDEX = "/run/sa02m-homekit-families/devices.json"


def _index_revision(st):
    # Readers must not authorize from a pre-validation writer's partial roster.
    return [5, *file_revision(st), *(file_fingerprint(PRODUCER) or ())]


def write_index(source, destination):
    """Publish the exact source revision and redacted family evidence."""
    content, source_stat = read_regular(source)
    revision = _index_revision(source_stat)
    families = admitted_families(parse_yaml(content))
    if families is None:
        raise ValueError("invalid bridge device list")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(destination))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            document = {"revision": revision, "led": families}
            if families.unidentified:
                document["unidentified"] = True
            json.dump(document, output, separators=(",", ":"))
        os.chmod(tmp, 0o640)
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_index(source):
    try:
        content, _ = read_regular(INDEX)
        index = json.loads(content)
        revision = _index_revision(os.stat(source, follow_symlinks=False))
        if isinstance(index, dict) and index.get("revision") == revision:
            families = index.get("led")
            unidentified = index.get("unidentified", False)
            if (type(unidentified) is bool and isinstance(families, dict)
                    and all(v is None or type(v) is bool for v in families.values())):
                return FamilyEvidence(families, unidentified=unidentified)
    except (OSError, ValueError, RecursionError):
        pass
    return None


def load_families(paths, load_yaml):
    """Prefer the installed configuration, even when only its index is readable.

    The second result marks an inaccessible source with unknown ownership.
    An unreadable installed file must never be replaced by the package example.
    """
    for path in paths:
        try:
            st = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            return None, True
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None, True
        families = _read_index(path)
        if families is not None:
            return families, False
        if os.access(path, os.R_OK):
            return admitted_families(load_yaml(path)), False
        return None, True
    return {}, False


def main():
    import sys

    if not os.path.exists(SOURCE):
        return
    import yaml
    try:
        write_index(SOURCE, INDEX)
    except FileNotFoundError:
        if os.path.exists(SOURCE):
            raise
    except (ValueError, UnicodeError, RecursionError, yaml.YAMLError):
        # The raw document and parser exceptions may carry broker credentials.
        print("Unreadable bridge device list; index not refreshed", file=sys.stderr)


if __name__ == "__main__":
    main()
