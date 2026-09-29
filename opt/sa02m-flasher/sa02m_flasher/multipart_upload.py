# -*- coding: utf-8 -*-
"""
multipart/form-data parsing for POST /firmware/upload - without the `cgi` module.

`cgi` is deprecated since Python 3.11 and removed in 3.13 (PEP 594); service.py
imported it at module level, so a distro upgrade would have stopped the whole
daemon (audit 2026-09-24 L9). The body is parsed with the stdlib `email.parser`
(present on every version) and the behaviour of the former cgi.FieldStorage path
is kept:

- the field is `file`; the file name is the `filename` parameter of the part's
  Content-Disposition as the browser sent it: part headers decoded as UTF-8
  (errors="replace"), parameters split by the cgi.parse_header rule (a quoted
  value is unquoted and its backslash-escaped quotes and backslashes undone; a
  `;` inside quotes does not split).
  A path in the name is NOT stripped - FirmwareRepo.add_upload() sanitises it;
- the file bytes are exactly the body bytes: no Content-Transfer-Encoding is
  applied, and the line break before the delimiter belongs to the delimiter;
- exactly Content-Length bytes are read; there is no size cap here (nginx holds
  it: `client_max_body_size` on /api/flasher/);
- the boundary is validated by the cgi.valid_boundary rule.

One deliberate difference: a second `file` field in one request is refused (400);
cgi returned a list there and the service failed with a 500.

Test: tests/test_firmware_upload_multipart.py (on Python <= 3.12 it also runs the
former cgi implementation as an oracle and requires identical results).
"""
from __future__ import annotations

import re
from email.message import Message
from email.parser import BytesParser
from email.policy import compat32
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

# cgi.valid_boundary: 1..201 printable ASCII characters, the last one not a space.
_VALID_BOUNDARY_RE = re.compile(r"^[ -~]{0,200}[!-~]$")


def read_file_field(headers: Mapping[str, Any], rfile: Any) -> Tuple[str, bytes]:
    """Вернуть (filename, raw_bytes) поля 'file' запроса multipart/form-data."""
    ctype = headers.get("Content-Type") or ""
    if not ctype.startswith("multipart/"):
        raise ValueError("Ожидается multipart/form-data")
    length = int(headers.get("Content-Length") or 0)
    if length <= 0:
        raise ValueError("Пустое тело запроса")
    return parse_file_field(ctype, rfile.read(length))


def parse_file_field(ctype: str, body: bytes) -> Tuple[str, bytes]:
    _, ct_params = _parse_header(ctype)
    boundary = ct_params.get("boundary", "")
    if not _VALID_BOUNDARY_RE.match(boundary):
        raise ValueError("Некорректный multipart/form-data: нет или неверная граница (boundary)")
    # compat32 + BytesParser: the body is held as ASCII/surrogateescape text, so
    # every byte round-trips exactly (see _raw_payload).
    msg = BytesParser(policy=compat32).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + body
    )
    parts = msg.get_payload() if msg.is_multipart() else []
    files = [p for p in parts if _disposition(p).get("name") == "file"]
    if not files:
        raise ValueError("Поле 'file' не найдено")
    if len(files) > 1:
        raise ValueError("Поле 'file' передано несколько раз")
    item = files[0]
    filename = _disposition(item).get("filename")
    if not filename:
        raise ValueError("Отсутствует имя файла")
    return filename, _raw_payload(item)


def _raw_payload(part: Message) -> bytes:
    # get_payload(decode=False) re-decodes 8-bit bytes through the part charset
    # with errors="replace" (bytes lost); decode=True hands back the exact bytes
    # but would also apply a Content-Transfer-Encoding, which cgi never did —
    # so the header is dropped first (browsers do not send one, RFC 7578 §4.7).
    del part["Content-Transfer-Encoding"]
    data = part.get_payload(decode=True)
    if not isinstance(data, bytes):
        raise ValueError("Поле 'file' не является файлом")
    return data


def _disposition(part: Message) -> Dict[str, str]:
    # raw_items(), not get(): get() wraps a header holding non-ASCII bytes in an
    # unknown-8bit Header object; the raw value still carries the bytes as
    # surrogates, which are undone here and decoded as cgi did (UTF-8, replace).
    raw: Optional[str] = next(
        (v for k, v in part.raw_items() if k.lower() == "content-disposition"), None
    )
    if raw is None:
        return {}
    text = raw.encode("ascii", "surrogateescape").decode("utf-8", "replace")
    return _parse_header(text)[1]


def _split_params(s: str) -> Iterator[str]:
    # A `;` inside a quoted value does not split (quotes counted, `\"` excluded).
    while s[:1] == ";":
        s = s[1:]
        end = s.find(";")
        while end > 0 and (s.count('"', 0, end) - s.count('\\"', 0, end)) % 2:
            end = s.find(";", end + 1)
        if end < 0:
            end = len(s)
        yield s[:end].strip()
        s = s[end:]


def _parse_header(line: str) -> Tuple[str, Dict[str, str]]:
    """`value; k=v; k="q"` -> (value, {k: v}) — the cgi.parse_header rule."""
    parts = _split_params(";" + line)
    key = next(parts)
    params: Dict[str, str] = {}
    for p in parts:
        i = p.find("=")
        if i >= 0:
            name = p[:i].strip().lower()
            value = p[i + 1:].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1].replace("\\\\", "\\").replace('\\"', '"')
            params[name] = value
    return key, params
