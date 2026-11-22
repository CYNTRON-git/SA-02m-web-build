"""One bounded MQTT QoS1 attempt, no reconnect queue and no retained commands."""
import socket
import struct
import uuid


def _string(value):
    raw = value.encode("utf-8")
    if not raw or len(raw) > 65535 or b"\0" in raw:
        raise ValueError("invalid MQTT string")
    return struct.pack("!H", len(raw)) + raw


def _packet(header, payload):
    size, encoded = len(payload), bytearray()
    while True:
        digit, size = size % 128, size // 128
        encoded.append(digit | (128 if size else 0))
        if not size:
            return bytes((header,)) + bytes(encoded) + payload


def _read_exact(sock, size):
    result = b""
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise ConnectionError("MQTT closed")
        result += chunk
    return result


def publish_once(topic, payload, *, host="127.0.0.1", port=1883, timeout=2):
    # The local broker endpoint is operator configuration, never request input.
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        connect = _string("MQTT") + bytes((4, 2, 0, 10)) + _string("home-" + uuid.uuid4().hex[:12])
        sock.sendall(_packet(0x10, connect))
        if _read_exact(sock, 4) != b"\x20\x02\x00\x00":
            raise ConnectionError("MQTT CONNACK refused")
        sock.sendall(_packet(0x32, _string(topic) + b"\x00\x01" + payload.encode("utf-8")))
        if _read_exact(sock, 4) != b"\x40\x02\x00\x01":
            raise ConnectionError("MQTT PUBACK missing")
        # A failed DISCONNECT cannot undo an already observed broker PUBACK.
        try:
            sock.sendall(b"\xe0\x00")
        except OSError:
            pass
