#!/bin/bash
# Remove legacy installer password banners without replacing or truncating logs.
# Full installs source this from scripts/lib.sh; OTA loads the installed copy.
# Python 3 is already required by both install-maintenance and the update runner.
#
# Only value bytes after the legacy prefix are overwritten, with equal-length
# stars. The inode, size, owner, mode and unrelated bytes stay intact, including
# concurrent appends and writers holding an open fd. Already masked values cause
# no write. Newlines and a trailing ANSI color reset are retained. Compressed
# rotations and symlinks are skipped. This covers append-only logs, not concurrent
# arbitrary rewrites/truncation. Errors never fail the install/update caller.
# Unexpected failures emit a path/errno-only stderr warning; callers persist it
# without reporting the failed file as successfully scrubbed.
# Reads are limited to the initial file size and use bounded 64 KiB chunks.
# Gate: web-install-secret.

sa02m_scrub_cred_banner() {  # FILE...
    python3 - "$@" <<'PY' || true
import os
import errno
import stat
import sys

PREFIX = '[OK]  Логин: admin / '.encode()
CHUNK = 65536

for path in sys.argv[1:]:
    if path.endswith(('.gz', '.xz', '.bz2', '.zst')):
        continue
    fd = None
    changed = False
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            continue
        limit = info.st_size
        offset = 0
        read_offset = 0
        buf = b''
        masking = False

        def mask(data, at, line_end=False):
            global changed
            if line_end:
                data = data.removesuffix(b'\r')
                data = data.removesuffix(b'\x1b[0m')
            if not data or data == b'*' * len(data):
                return
            # Do not grow a log that was truncated since the initial snapshot.
            if os.fstat(fd).st_size < at + len(data):
                raise OSError('log truncated during scrub')
            value = b'*' * len(data)
            done = 0
            while done < len(value):
                n = os.pwrite(fd, value[done:], at + done)
                if n <= 0:
                    raise OSError('short scrub write')
                done += n
            changed = True

        while read_offset < limit or buf:
            if read_offset < limit:
                part = os.pread(fd, min(CHUNK, limit - read_offset), read_offset)
                if not part:
                    break
                read_offset += len(part)
                buf += part
            final = read_offset >= limit
            while buf:
                newline = buf.find(b'\n')
                if masking:
                    if newline >= 0:
                        mask(buf[:newline], offset, line_end=True)
                        n = newline + 1
                        masking = False
                    elif final:
                        mask(buf, offset, line_end=True)
                        n = len(buf)
                    else:
                        # Retain CR + the four-byte color reset across chunks.
                        n = max(0, len(buf) - 5)
                        mask(buf[:n], offset)
                else:
                    start = buf.find(PREFIX)
                    if start >= 0 and (newline < 0 or start < newline):
                        n = start + len(PREFIX)
                        masking = True
                    elif newline >= 0:
                        n = newline + 1
                    else:
                        n = len(buf) if final else max(0, len(buf) - len(PREFIX) + 1)
                if not n:
                    break
                buf = buf[n:]
                offset += n
            if final:
                break
        if changed:
            os.fsync(fd)
            print(path)
    except OSError as exc:
        # Missing paths and refused symlinks are expected exclusions. Never
        # print exception text: it can include data supplied by a failing tool.
        if fd is not None or exc.errno not in (errno.ENOENT, errno.ELOOP):
            print(f'scrub error: path={ascii(path)} errno={exc.errno}', file=sys.stderr)
    finally:
        if fd is not None:
            os.close(fd)
PY
    return 0
}

# No rotation rule ships today; include plain numbered rotations if present.
sa02m_scrub_cred_banner_logs() {
    sa02m_scrub_cred_banner \
        /var/log/sa02m_install.log /var/log/sa02m_install.log.[0-9]* \
        /var/lib/sa02m-web-build/update.log /var/lib/sa02m-web-build/update.log.[0-9]*
}
