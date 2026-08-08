# -*- coding: utf-8 -*-
"""Streaming multipart upload receiver for package.sa02m (stdlib only, no cgi)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional, Tuple, Union

try:
    from . import PackageError
    from .validate_package import validate_container
except ImportError:  # pragma: no cover
    from __init__ import PackageError  # type: ignore
    from validate_package import validate_container  # type: ignore

DEFAULT_STATEDIR = Path("/var/lib/sa02m-update")
DEFAULT_INCOMING = DEFAULT_STATEDIR / "incoming"
PARTIAL_NAME = "package.partial"
FINAL_NAME = "package.sa02m"
DEFAULT_MAX_FILE = 128 << 20  # 128 MiB
MULTIPART_OVERHEAD = 8192

_BOUNDARY_RE = re.compile(rb'boundary=(?:"([^"]+)"|([^;]+))', re.IGNORECASE)


def parse_multipart_boundary(content_type: Union[str, bytes]) -> bytes:
    raw = content_type.encode("latin-1") if isinstance(content_type, str) else content_type
    m = _BOUNDARY_RE.search(raw)
    if not m:
        raise PackageError("E_UPLOAD", "multipart boundary missing")
    boundary = m.group(1) or m.group(2)
    boundary = boundary.strip()
    if not boundary or len(boundary) > 200:
        raise PackageError("E_UPLOAD", "multipart boundary invalid")
    return boundary


class _Pushback:
    __slots__ = ("_raw", "_buf")

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._buf = b""

    def unread(self, data: bytes) -> None:
        if data:
            self._buf = data + self._buf

    def read(self, n: int = -1) -> bytes:
        if n == 0:
            return b""
        if self._buf:
            if n is None or n < 0:
                data, self._buf = self._buf, b""
                rest = self._raw.read() if n is None or n < 0 else b""
                return data + rest
            if len(self._buf) >= n:
                data, self._buf = self._buf[:n], self._buf[n:]
                return data
            need = n - len(self._buf)
            data = self._buf
            self._buf = b""
            return data + self._raw.read(need)
        return self._raw.read(n if n is not None else -1)


def _read_headers(stream: _Pushback, *, limit: int = 8192) -> Dict[str, str]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = stream.read(256)
        if not chunk:
            raise PackageError("E_UPLOAD", "EOF while reading multipart headers")
        data.extend(chunk)
        if len(data) > limit:
            raise PackageError("E_UPLOAD", "multipart headers too large")
    idx = data.index(b"\r\n\r\n")
    header_blob = bytes(data[:idx])
    stream.unread(bytes(data[idx + 4 :]))
    headers: Dict[str, str] = {}
    for line in header_blob.split(b"\r\n"):
        if b":" not in line:
            continue
        k, v = line.split(b":", 1)
        headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    return headers


def _content_disposition_name(value: str) -> Tuple[Optional[str], Optional[str]]:
    # name="file"; filename="x.sa02m"
    name = None
    filename = None
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("name="):
            name = part.split("=", 1)[1].strip().strip('"')
        elif part.lower().startswith("filename="):
            filename = part.split("=", 1)[1].strip().strip('"')
    return name, filename


def iter_part_body(stream: _Pushback, boundary: bytes, *, max_file: int):
    """Yield body chunks until CRLF--boundary; do NOT trust CONTENT_LENGTH remaining."""
    delim = b"\r\n--" + boundary
    window = b""
    total = 0
    while True:
        if delim in window:
            idx = window.index(delim)
            chunk = window[:idx]
            if chunk:
                total += len(chunk)
                if total > max_file:
                    raise PackageError("E_UPLOAD", f"file exceeds max_file ({max_file})")
                yield chunk
            rest = window[idx + len(delim) :]
            stream.unread(rest)
            return
        # Keep possible partial delimiter
        keep = len(delim) - 1
        if len(window) > keep:
            emit = window[:-keep]
            window = window[-keep:]
            if emit:
                total += len(emit)
                if total > max_file:
                    raise PackageError("E_UPLOAD", f"file exceeds max_file ({max_file})")
                yield emit
        chunk = stream.read(65536)
        if not chunk:
            raise PackageError("E_UPLOAD", "unexpected EOF before multipart boundary")
        window += chunk


def _fsync_dir(dir_path: Path) -> None:
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def receive_multipart_file(
    environ: MutableMapping[str, object],
    *,
    field: str = "file",
    max_file: int = DEFAULT_MAX_FILE,
    incoming_dir: Path = DEFAULT_INCOMING,
    validate: bool = True,
) -> Dict[str, object]:
    """Stream multipart field to incoming/package.partial → package.sa02m.

    Adversarial rules:
      - do not use cgi multipart parsers
      - body end detected by boundary, not CONTENT_LENGTH remaining
      - CONTENT_LENGTH only used as an upper cap (max_file + overhead)
      - validate_container on FINAL path only
    """
    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    if method != "POST":
        raise PackageError("E_UPLOAD", f"POST required, got {method}")

    content_type = environ.get("CONTENT_TYPE") or environ.get("CONTENT_TYPE".title())
    if not content_type:
        # WSGI often uses CONTENT_TYPE
        content_type = environ.get("CONTENT_TYPE", "")
    content_type_s = str(content_type)
    if "multipart/form-data" not in content_type_s.lower():
        raise PackageError("E_UPLOAD", "Content-Type must be multipart/form-data")

    boundary = parse_multipart_boundary(content_type_s)

    try:
        cl = int(str(environ.get("CONTENT_LENGTH") or "0"))
    except ValueError as exc:
        raise PackageError("E_UPLOAD", "CONTENT_LENGTH invalid") from exc
    if cl < 1:
        raise PackageError("E_UPLOAD", "CONTENT_LENGTH required")
    if cl > max_file + MULTIPART_OVERHEAD:
        raise PackageError("E_UPLOAD", f"CONTENT_LENGTH {cl} exceeds cap")

    wsgi_input = environ.get("wsgi.input")
    if wsgi_input is None or not hasattr(wsgi_input, "read"):
        raise PackageError("E_UPLOAD", "wsgi.input missing")

    incoming_dir = Path(incoming_dir)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    partial = incoming_dir / PARTIAL_NAME
    final = incoming_dir / FINAL_NAME

    stream = _Pushback(wsgi_input)  # type: ignore[arg-type]

    # Preamble until first boundary
    first = b"--" + boundary
    preamble = bytearray()
    while first not in preamble:
        chunk = stream.read(1024)
        if not chunk:
            raise PackageError("E_UPLOAD", "multipart start boundary not found")
        preamble.extend(chunk)
        if len(preamble) > MULTIPART_OVERHEAD:
            raise PackageError("E_UPLOAD", "multipart preamble too large")
    idx = preamble.index(first)
    after = bytes(preamble[idx + len(first) :])
    stream.unread(after)
    # Expect \r\n after boundary (or -- for end)
    head = stream.read(2)
    if head == b"--":
        raise PackageError("E_UPLOAD", "empty multipart body")
    if head != b"\r\n":
        stream.unread(head)

    found = False
    size = 0
    while True:
        headers = _read_headers(stream)
        disp = headers.get("content-disposition", "")
        name, _filename = _content_disposition_name(disp)
        if name == field:
            found = True
            # Write body to partial
            if partial.exists():
                partial.unlink()
            with partial.open("wb") as out:
                for chunk in iter_part_body(stream, boundary, max_file=max_file):
                    out.write(chunk)
                    size += len(chunk)
                out.flush()
                try:
                    os.fdatasync(out.fileno())
                except (AttributeError, OSError):
                    os.fsync(out.fileno())
            # After body: boundary may be followed by \r\n (next part) or -- (end)
            trail = stream.read(2)
            if trail == b"--":
                break
            if trail != b"\r\n":
                stream.unread(trail)
            # continue to drain other parts
            continue

        # Drain non-target part
        for _ in iter_part_body(stream, boundary, max_file=max_file):
            pass
        trail = stream.read(2)
        if trail == b"--":
            break
        if trail != b"\r\n":
            stream.unread(trail)

    if not found:
        if partial.exists():
            partial.unlink(missing_ok=True)  # type: ignore[call-arg]
        raise PackageError("E_UPLOAD", f"multipart field {field!r} not found")

    if size < 1:
        partial.unlink(missing_ok=True)  # type: ignore[call-arg]
        raise PackageError("E_UPLOAD", "empty upload")

    os.replace(str(partial), str(final))
    _fsync_dir(incoming_dir)

    result: Dict[str, object] = {"ok": True, "size": size, "path": str(final)}
    if validate:
        tar_size = validate_container(final)
        result["tar_size"] = tar_size
    return result
