# sa02m-homekit unit tests (stdlib unittest, offline).
#
# Run: python3 -m unittest discover -s opt/sa02m-homekit/tests -t opt/sa02m-homekit
# The package consumes the Alice DeviceRegistry one-way (homekit-bridge.md
# §Зависимости), so the sibling package root goes on sys.path here. Tests that
# need HAP-python / segno skip LOUDLY when those are absent (tests/_deps.py);
# every other test is pure and runs on the stock interpreter.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
HOMEKIT_ROOT = os.path.dirname(_HERE)
ALICE_ROOT = os.path.abspath(os.path.join(HOMEKIT_ROOT, "..", "sa02m-alice"))
REPO_ROOT = os.path.abspath(os.path.join(HOMEKIT_ROOT, "..", ".."))
for _p in (HOMEKIT_ROOT, ALICE_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
