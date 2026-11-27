"""Closed canonical pending-completion requests, with no captured generation."""

import base64
import binascii
import hashlib
import json
import re

from . import event_journal_delivery as files
from . import event_journal_source as source
from . import event_journal_state as state

SETTINGS = ("STAND_DEVICES_EVENTS_STATE_DIR", "STAND_DEVICES_EVENTS_BOOTSTRAP_DIR",
            "STAND_DEVICES_EVENTS_UPDATE_DIR", "STAND_DEVICES_EVENTS_PROTOCOL")
KEYS = {"version", "settings", "identity", "fingerprint", "context", "locks"}
PREFIX = "sa02m-journal-complete-"


def validate(value):
    if not isinstance(value, dict) or set(value) != KEYS or type(value["version"]) is not int or value["version"] != 1:
        raise state.JournalStateError("invalid journal completion request schema")
    if value["identity"] == "legacy" or not state.valid_generation(value["identity"]) \
            or not isinstance(value["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"]):
        raise state.JournalStateError("invalid journal completion source identity")
    settings = value["settings"]
    if not isinstance(settings, dict) or set(settings) != set(SETTINGS) \
            or any(not isinstance(setting, str) for setting in settings.values()) \
            or settings[SETTINGS[-1]] != "1":
        raise state.JournalStateError("invalid journal completion settings")
    for key in SETTINGS[:-1]:
        files.canonical_directory(settings[key])
    authority, receipt = (files.canonical_directory(settings[key]) for key in SETTINGS[:2])
    if authority == receipt or authority in receipt.parents or receipt in authority.parents:
        raise state.JournalStateError("journal receipt requires an independent directory")
    files.validate_source_context(value["context"])
    locks = value["locks"]
    if not isinstance(locks, dict) or set(locks) != {"install", "update"}:
        raise state.JournalStateError("invalid journal completion lock set")
    for info in locks.values():
        if not isinstance(info, dict) or set(info) != {"device", "inode", "mode"} \
                or any(type(item) is not int for item in info.values()) \
                or info["device"] < 0 or info["inode"] <= 0 or not 0 <= info["mode"] <= 0o7777:
            raise state.JournalStateError("invalid journal completion lock identity")


def encode(value):
    validate(value)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    if len(encoded) > 4096:
        raise state.JournalStateError("journal completion request exceeded bound")
    return encoded


def decode(encoded):
    if not isinstance(encoded, str) or len(encoded) > 4096:
        raise state.JournalStateError("journal completion request exceeded bound")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("ascii"), object_pairs_hook=files.unique_pairs)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise state.JournalStateError("malformed journal completion request") from exc
    if encode(value) != encoded:
        raise state.JournalStateError("noncanonical journal completion request")
    return value


def unit_name(encoded):
    decode(encoded)
    return PREFIX + hashlib.sha256(encoded.encode("ascii")).hexdigest() + ".service"


def build(config, identity, deadline):
    value = {"version": 1, "settings": config.settings(), "identity": identity,
             "fingerprint": source.verified_fingerprint(source.installed_root(), deadline),
             "context": files.source_context(config.update), "locks": files.completion_lock_records(config.update)}
    return encode(value)
