"""docs/contracts/cloud-agent-status.md — the status-file state enum, end to end.

The enum has ONE home in code (agent.STATUS_STATES). The contract's enum line,
every `_write_status("…")` literal in the agent source, the two states derived
from a refusal class, and the card's CLOUD_STATE_MAP must all agree with it —
so a state added in any one place without the others fails here.
"""
import importlib.util
import os
import re

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.environ.get("SA02M_AGENT_PATH") or os.path.join(AGENT_DIR, "sa02m-cloud-agent.py")
REPO = os.path.abspath(os.path.join(AGENT_DIR, "..", ".."))

_spec = importlib.util.spec_from_file_location("sa02m_cloud_agent_status_contract", AGENT_PATH)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

with open(AGENT_PATH, encoding="utf-8") as _f:
    AGENT_SOURCE = _f.read()


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def test_status_state_enum_matches_contract_and_card():
    enum = set(agent.STATUS_STATES)
    assert enum, "STATUS_STATES is empty"

    doc = _read("docs", "contracts", "cloud-agent-status.md")
    m = re.search(r"`state ∈ ([^`]+)`", doc)
    assert m, "the contract has no `state ∈ …` enum line"
    documented = {x.strip() for x in m.group(1).split("|")}
    assert documented == enum, "contract enum != agent.STATUS_STATES: %s" % sorted(documented ^ enum)

    written = set(re.findall(r'_write_status\("([a-z_]+)"', AGENT_SOURCE))
    written |= {agent._stand_down_state(c) for c in ("revoked", "unlinked", "unknown")}
    assert written == enum, "states the agent writes != STATUS_STATES: %s" % sorted(written ^ enum)

    js = _read("www", "network_config", "static", "js", "cloud.js")
    block = js[js.index("const CLOUD_STATE_MAP = {"):]
    block = block[:block.index("};")]
    card = set(re.findall(r"^\s*([a-z_]+):\s*\[", block, re.M))
    legacy = {"activating", "activation_failed", "unknown"}
    assert enum <= card, "card has no label for: %s" % sorted(enum - card)
    assert card - enum <= legacy, "card labels states the agent never writes: %s" % sorted(card - enum - legacy)


def test_live_only_keys_are_documented_and_pinned():
    doc = _read("docs", "contracts", "cloud-agent-status.md")
    for key in agent.LIVE_ONLY_KEYS:
        assert re.search(r"`%s`" % re.escape(key), doc), "%s is not documented" % key
    # The pins the contract names must exist by that name.
    tests = (_read("opt", "sa02m-cloud-agent", "tests", "test_revoke_standdown.py")
             + _read("opt", "sa02m-cloud-agent", "tests", "test_status_contract.py"))
    cited = set(re.findall(r"::(test_[a-z_]+)", doc))
    assert cited, "the contract cites no pinning test"
    for name in sorted(cited):
        assert ("def %s(" % name) in tests, "contract cites a test that does not exist: %s" % name


def test_stand_down_marker_keys_are_documented():
    doc = _read("docs", "contracts", "cloud-agent-status.md")
    for key in agent.STAND_DOWN_MARKER_KEYS:
        assert ("`%s`" % key) in doc, key
    assert "image-identity-reset.md" in doc
