"""Explicit private authority resources for unprivileged composition tests.

Only the expected local owner changes. Shared scope and fail-closed missing
state behavior stay intact; production root ownership is exercised separately
by the privileged delivery/permission harness.
"""

from functools import wraps
import os

import pytest

from sa02m_devices import event_journal_state as journal


class OwnedDomain(journal.Domain):
    @property
    def owner(self):
        return os.geteuid()


_domain_for = journal.domain_for


def private_domain_for(path=None):
    domain = _domain_for(path)
    return OwnedDomain(domain.directory, domain.standalone, domain.archive)


@pytest.fixture
def private_authority(monkeypatch):
    monkeypatch.setattr(journal, "domain_for", private_domain_for)


def install_private_authority():
    journal.domain_for = private_domain_for


def private_process(function):
    @wraps(function)
    def run(*args, **kwargs):
        previous = journal.domain_for
        install_private_authority()
        try:
            return function(*args, **kwargs)
        finally:
            journal.domain_for = previous
    return run
