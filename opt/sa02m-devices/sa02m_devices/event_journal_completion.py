"""Exclusion-admitted completion; stale workers never mutate a newer authority."""

import os
from pathlib import Path
import time

from . import event_journal_delivery as files
from . import event_journal_peer as peer
from . import event_journal_request as request
from . import event_journal_source as source
from . import event_journal_startup as startup
from . import event_journal_state as state


def _source(config, value, deadline):
    if os.path.lexists("/run/sa02m-imaging.lock"):
        raise state.JournalBusy("journal completion waits for image maintenance")
    files.require_source_context(config.update, value["context"])
    if source.verified_fingerprint(source.installed_root(), deadline) != value["fingerprint"]:
        raise state.JournalStateError("journal completion installed source changed")


def complete(encoded):
    value = request.decode(encoded)
    settings = value["settings"]
    config = startup.Configuration(state.Domain(Path(settings[request.SETTINGS[0]])),
                                   Path(settings[request.SETTINGS[1]]), Path(settings[request.SETTINGS[2]]))
    with files.completion_admission(config.update, value["locks"]):
        admission_deadline = time.monotonic() + 5
        _source(config, value, admission_deadline)
        with startup.receipt_guard(config, deadline=admission_deadline, create=False):
            deadline = time.monotonic() + 5
            current = startup.validate(config, deadline=deadline)
            if current.identity != value["identity"]:
                raise state.JournalStateError("journal completion authority changed")
            observed = peer.observe(config.settings(), deadline=deadline)
            _, _, peer_current = observed
            # Equivalent completed work is idempotent; a pending hook established false.
            if current.ready and peer_current:
                _source(config, value, time.monotonic() + 5)
                return current
            def not_ready():
                return startup.set_ready_for(config, value["identity"], False, time.monotonic() + 1)
            peer.transition(config.settings(), not_ready,
                            force_refresh=value["context"]["mode"] == "pending", observed=observed, deadline=deadline)
            deadline = time.monotonic() + 5
            _source(config, value, deadline)
            current = startup.validate(config, deadline=deadline)
            _, _, peer_current = peer.observe(config.settings(), deadline=deadline)
            if not peer_current:
                raise state.JournalStateError("journal completion peer postcondition failed")
            return startup.set_ready_for(config, value["identity"], True, deadline)
