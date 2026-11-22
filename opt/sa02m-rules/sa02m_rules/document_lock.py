"""Reentrant bounded directory lock shared by every scenario command writer."""
import os
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # Windows development tests have no POSIX processes.
    fcntl = None

_local = threading.local()
_threads = threading.RLock()


@contextmanager
def command_lock(path="/etc/sa02m-rules/scenarios.json"):
    directory = os.path.abspath(os.path.dirname(path) or ".")
    with _threads:
        depths = getattr(_local, "depths", {})
        _local.depths = depths
        if depths.get(directory):
            depths[directory] += 1
            try:
                yield
            finally:
                depths[directory] -= 1
            return
        os.makedirs(directory, exist_ok=True)
        fd = None
        try:
            if fcntl is not None:
                fd = os.open(directory, os.O_RDONLY)
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("scenario document busy")
                        time.sleep(.02)
            depths[directory] = 1
            yield
        finally:
            depths.pop(directory, None)
            if fd is not None:
                os.close(fd)
