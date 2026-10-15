# sa02m-audio unit tests (stdlib unittest, offline).
#
# Run: python3 -m unittest discover -s opt/sa02m-audio/tests -t opt/sa02m-audio
# paho is replaced by an in-memory client fake (fakes.FakePaho), arecord by a
# fake script put first on PATH (fakes.fake_arecord_dir). Cases that need
# POSIX (fcntl leases, dir_fd, process groups, the real `timeout`) skip off
# POSIX and say so; the authoritative run is Linux (CI, WSL, the board).
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_ROOT = os.path.dirname(_HERE)
REPO_ROOT = os.path.abspath(os.path.join(AUDIO_ROOT, "..", ".."))
if AUDIO_ROOT not in sys.path:
    sys.path.insert(0, AUDIO_ROOT)

POSIX = os.name == "posix"
POSIX_REASON = "needs POSIX (fcntl / dir_fd / process groups) — runs on Linux CI, WSL, the board"
