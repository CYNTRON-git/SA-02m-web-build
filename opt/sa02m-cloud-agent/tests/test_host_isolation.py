"""The suite never writes the host's /var/log: the agent's import-time log file lands in the
session sandbox that conftest.py installs (the why is in conftest.py's docstring).

Non-vacuous: the agent must really have created a file handler, and it must be inside the sandbox — a
missing conftest, a redirect that no longer matches the agent's path, or a handler the agent stopped
creating all FAIL here instead of passing on zero handlers.
"""
import importlib.util
import logging
import os

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")


def test_agent_log_file_is_in_the_session_sandbox():
    sandbox = os.environ.get("SA02M_TEST_LOG_SANDBOX", "")
    assert sandbox and os.path.isdir(sandbox), "conftest.py did not install the log sandbox"
    spec = importlib.util.spec_from_file_location("sa02m_cloud_agent_isolation", AGENT_PATH)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    files = [h for h in agent._handlers if isinstance(h, logging.FileHandler)]
    try:
        assert files, "the agent created no file handler — its /var/log handler was not redirected"
        for h in files:
            where = os.path.realpath(h.baseFilename)
            assert os.path.commonpath([where, os.path.realpath(sandbox)]) == os.path.realpath(sandbox), \
                f"agent log file outside the test sandbox: {h.baseFilename}"
    finally:
        for h in files:
            h.close()
