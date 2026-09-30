# sa02m-homeconnect unit tests (stdlib unittest, offline).
#
# Run: python3 -m unittest discover -s opt/sa02m-homeconnect/tests -t opt/sa02m-homeconnect
# The BSH cloud is a local http.server fake (tests/fake_bsh.py) bound to
# 127.0.0.1; paho is replaced by an in-memory broker fake. No network, no pip.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
HOMECONNECT_ROOT = os.path.dirname(_HERE)
REPO_ROOT = os.path.abspath(os.path.join(HOMECONNECT_ROOT, "..", ".."))
if HOMECONNECT_ROOT not in sys.path:
    sys.path.insert(0, HOMECONNECT_ROOT)
