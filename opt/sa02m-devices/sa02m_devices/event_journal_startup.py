"""Explicit service provisioning and administrative recovery of journal authority."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import secrets
import sys
import time

from . import event_journal_state as state
from . import event_journal_delivery as files
from . import event_journal_peer as peer
from . import event_journal_source as source

DEFAULT_RECEIPTS = Path("/var/lib/sa02m-devices-journal-init")
DEFAULT_UPDATE = Path("/var/lib/sa02m-update")


@dataclass(frozen=True)
class Configuration:
    domain: state.Domain
    receipts: Path
    update: Path | None

    def settings(self):
        return {"STAND_DEVICES_EVENTS_STATE_DIR": str(self.domain.directory),
                "STAND_DEVICES_EVENTS_BOOTSTRAP_DIR": str(self.receipts),
                "STAND_DEVICES_EVENTS_UPDATE_DIR": str(self.update) if self.update else "",
                "STAND_DEVICES_EVENTS_PROTOCOL": "1"}

    @classmethod
    def from_environment(cls):
        def path(name, default):
            return files.canonical_directory(os.environ.get(name, str(default)))
        if os.environ.get("STAND_DEVICES_EVENTS_PROTOCOL", "1") != "1":
            raise ValueError("STAND_DEVICES_EVENTS_PROTOCOL must be 1")
        return cls(state.Domain(path("STAND_DEVICES_EVENTS_STATE_DIR", state.DEFAULT_DIRECTORY)),
                   path("STAND_DEVICES_EVENTS_BOOTSTRAP_DIR", DEFAULT_RECEIPTS),
                   path("STAND_DEVICES_EVENTS_UPDATE_DIR", DEFAULT_UPDATE))


def _checkpoint(stage):
    """Crash-injection seam at durable protocol boundaries; no production side effect."""


def _receipt(config, identity, phase):
    return {"version": 1, "identity": identity,
            "directory": str(config.domain.directory), "phase": phase}


def read_receipt(config, dfd):
    value = files.read_json(dfd, "receipt.json", config.domain.owner)
    if value is None:
        return None
    if set(value) != {"version", "identity", "directory", "phase"} \
            or type(value["version"]) is not int or value["version"] != 1 \
            or value["identity"] == "legacy" or not state.valid_generation(value["identity"]) \
            or value["directory"] != str(config.domain.directory) \
            or not isinstance(value["phase"], str) \
            or value["phase"] not in {"initializing", "initialized"}:
        raise state.JournalStateError("invalid journal bootstrap receipt")
    return value


@contextmanager
def receipt_guard(config, *, deadline, create=True):
    if config.receipts == config.domain.directory \
            or config.domain.directory in config.receipts.parents \
            or config.receipts in config.domain.directory.parents:
        raise state.JournalStateError("journal receipt requires an independent directory")
    files.canonical_directory(config.domain.directory)
    with files.directory(config.receipts, config.domain.owner, create=create) as dfd:
        fd = files.open_file(dfd, "bootstrap.lock", config.domain.owner, create=create)
        try:
            os.fsync(dfd)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = state.remaining(deadline)
                    if not remaining:
                        raise state.JournalBusy("journal bootstrap busy") from None
                    time.sleep(min(0.01, remaining))
            yield dfd
        finally:
            os.close(fd)


def _provision(config, dfd, deadline):
    receipt = read_receipt(config, dfd)
    if receipt is None:
        if set(os.listdir(dfd)) != {"bootstrap.lock"}:
            raise state.JournalStateError("unexplained journal bootstrap artifact")
        if os.path.lexists(config.domain.directory):
            with state.guard(config.domain, deadline=deadline) as current:
                files.publish_json(dfd, "receipt.json", _receipt(config, current.identity, "initialized"))
            return current
        receipt = _receipt(config, secrets.token_hex(16), "initializing")
        files.publish_json(dfd, "receipt.json", receipt)
        _checkpoint("receipt_initializing")
    if receipt["phase"] == "initializing":
        current = state.initialize_recorded(config.domain, receipt["identity"], deadline=deadline)
        _checkpoint("authority")
        files.publish_json(dfd, "receipt.json", _receipt(config, current.identity, "initialized"))
        _checkpoint("receipt_initialized")
    with state.guard(config.domain, deadline=deadline) as current:
        if current.identity != receipt["identity"]:
            raise state.JournalStateError("journal authority does not match bootstrap receipt")
        return current


def validate(config, *, deadline=None):
    """Read-only peer hook: no bootstrap mutex, no initialization, false-ready is valid."""
    with files.directory(config.receipts, config.domain.owner) as dfd:
        receipt = read_receipt(config, dfd)
    if receipt is None or receipt["phase"] != "initialized":
        raise state.JournalStateError("journal bootstrap has not completed")
    with state.guard(config.domain, deadline=deadline) as current:
        if receipt["identity"] != current.identity:
            raise state.JournalStateError("journal authority does not match bootstrap receipt")
        return current


def set_ready_for(config, identity, ready, deadline):
    with state.guard(config.domain, exclusive=True, deadline=deadline) as current:
        if current.identity != identity:
            raise state.JournalStateError("journal preparation identity changed")
        if current.ready == ready:
            return current
        return state.set_ready(config.domain, ready, deadline=deadline)


def prepare(config, consumer, *, rootfs=False):
    if consumer not in {"devices-api", "devices-logger", "installer"} \
            or (rootfs and consumer != "installer"):
        raise ValueError("invalid prepare consumer or rootfs mode")
    # The lock/init budget is shared, followed by 5 s probes + 35 s peer + 5 s final.
    initialization_deadline = time.monotonic() + 5
    with receipt_guard(config, deadline=initialization_deadline) as dfd:
        current = _provision(config, dfd, initialization_deadline)
        def not_ready():
            nonlocal current
            current = set_ready_for(config, current.identity, False, time.monotonic() + 1)
        try:
            if not rootfs and consumer != "installer":
                from . import event_journal_request as request
                from . import event_journal_queue as queue
                deadline = time.monotonic() + 5
                budget = peer.Budget(deadline)
                context = files.source_context(config.update)
                _, _, peer_current = peer.observe(config.settings(), deadline=deadline, budget=budget)
                if current.ready and peer_current and context["mode"] != "pending":
                    return current
                not_ready()
                encoded = request.build(config, current.identity, deadline)
                if request.decode(encoded)["context"] != context:
                    raise state.JournalStateError("journal updater context changed before enqueue")
                unit = queue.enqueue(encoded, deadline=deadline, budget=budget)
                _checkpoint("pending")
                print("journal preparation pending: " + unit)
                return current
            if not rootfs:
                deadline = time.monotonic() + 5
                fingerprint = source.verified_fingerprint(source.installed_root(), deadline)
                context = files.source_context(config.update)
                peer.transition(config.settings(), not_ready, force_refresh=True)
            _checkpoint("peer")
            deadline = time.monotonic() + 5
            validate(config, deadline=deadline)
            if not rootfs:
                if source.verified_fingerprint(source.installed_root(), deadline) != fingerprint \
                        or files.source_context(config.update) != context:
                    raise state.JournalStateError("journal installer source changed")
                peer.observe(config.settings(), deadline=deadline)
            current = set_ready_for(config, current.identity, True, deadline)
            _checkpoint("ready")
            return current
        except Exception:
            not_ready()
            raise


def recover(config):
    """Administrative call with consumers stopped; receipt is never a generation backup."""
    deadline = time.monotonic() + 5
    with receipt_guard(config, deadline=deadline) as dfd:
        current = state.recover(config.domain, deadline=deadline)
        _checkpoint("recovered_authority")
        files.publish_json(dfd, "receipt.json", _receipt(config, current.identity, "initialized"))
        return current


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "validate", "recover", "complete"))
    parser.add_argument("--consumer", choices=("devices-api", "devices-logger", "stand-api", "installer"))
    parser.add_argument("--rootfs", action="store_true")
    parser.add_argument("--request")
    args = parser.parse_args(argv)
    if (args.action == "prepare" and args.consumer not in {"devices-api", "devices-logger", "installer"}) \
            or (args.action == "validate" and args.consumer != "stand-api") \
            or (args.rootfs and (args.action != "prepare" or args.consumer != "installer")) \
            or (args.action in {"recover", "complete"} and args.consumer is not None) \
            or ((args.action == "complete") != (args.request is not None)):
        parser.error("invalid action/consumer combination")
    if args.action == "complete":
        from .event_journal_completion import complete
        try:
            complete(args.request)
            from .event_journal_request import unit_name
            print("journal completion ready: " + unit_name(args.request))
        except (state.JournalStateError, state.JournalBusy, OSError, ValueError) as exc:
            print("journal completion unavailable: " + str(exc)[:240], file=sys.stderr)
            return 1
        return 0
    try:
        config = Configuration.from_environment()
    except (ValueError, state.JournalStateError):
        parser.error("invalid journal environment configuration")
    try:
        if args.action == "prepare":
            prepare(config, args.consumer, rootfs=args.rootfs)
        elif args.action == "validate":
            validate(config)
        else:
            recover(config)
    except (state.JournalStateError, state.JournalBusy, OSError, ValueError) as exc:
        print("journal startup unavailable: " + str(exc)[:240], file=sys.stderr)
        return 1
    return 0
