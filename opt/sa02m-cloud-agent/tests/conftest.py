"""Test-session isolation for the cloud-agent suite: the host's /var/log stays out of reach.

sa02m-cloud-agent.py opens /var/log/sa02m-cloud-agent.log (logging.FileHandler, mode "a") at IMPORT
time, and every test module imports the agent by file path. Wherever /var/log is writable — a root
shell, a container, a CI runner under sudo — that created the host's log file and appended each test's
records to it. Before any test module is collected, logging.FileHandler is swapped for a subclass that
re-homes a /var/log/ path into a per-session temp dir; test_host_isolation.py FAILS when the agent's
handler did not land there.
"""
import atexit
import logging
import os
import shutil
import tempfile

SANDBOX = tempfile.mkdtemp(prefix="sa02m-cloud-agent-tests-")
os.environ["SA02M_TEST_LOG_SANDBOX"] = SANDBOX

_HostFileHandler = logging.FileHandler


class _SandboxedFileHandler(_HostFileHandler):
    def __init__(self, filename, *args, **kwargs):
        name = os.fspath(filename)
        # A string prefix, not os.path.isabs: on Windows "/var/log/..." is not absolute.
        if name.startswith("/var/log/"):
            name = os.path.join(SANDBOX, os.path.basename(name))
        super().__init__(name, *args, **kwargs)


logging.FileHandler = _SandboxedFileHandler


def _remove_sandbox():
    logging.shutdown()  # close the handlers first: Windows cannot delete an open file
    shutil.rmtree(SANDBOX, ignore_errors=True)


atexit.register(_remove_sandbox)
