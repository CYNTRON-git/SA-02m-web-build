"""Cloud-control's private root-peer socket hop; transport IDs never dedupe IDs."""
import http.client
import json
import socket

from ..client.sio_connection import EVT_DEVICES_RESPONSE
from .contract import APIError, MAX_RESPONSE, encoded, validate_request

SOCKET_PATH = "/run/sa02m-home/api.sock"
# Engine.IO's default incoming message limit on the shared Cloud gateway.
CLOUD_PACKET_BYTES = 1_000_000


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path=SOCKET_PATH, timeout=8):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def request(data, connection_factory=UnixConnection):
    is_command = False
    dispatch_started = False
    try:
        if not isinstance(data, dict):
            raise APIError(400, "invalid_request")
        method, path = data.get("method"), data.get("path")
        validate_request(method, path, data.get("query"), data.get("body"))
        is_command = method == "POST" and path == "/commands"
        envelope = {k: data.get(k) for k in ("method", "path", "query", "body")}
        outgoing = encoded(envelope)
        connection = connection_factory()
        try:
            connection.connect()
            dispatch_started = True
            connection.request("POST", "/internal/home/v1", outgoing, {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise APIError(503, "invalid_board_response")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError()
            return {"status": response.status, "body": body}
        finally:
            connection.close()
    except APIError as exc:
        status, code = exc.status, exc.code
    except (OSError, ValueError, http.client.HTTPException):
        status, code = 503, "home_api_unavailable"
    body = {"ok": False, "error": code}
    if is_command:
        body["outcome_unknown"] = dispatch_started
    return {"status": status, "body": body}


def cloud_reply(data):
    response = {"request_id": data.get("request_id"), "payload": request(data)}
    # On the default namespace an event has two type bytes followed by compact
    # ASCII-escaped JSON. Count the envelope too, before handing it to Socket.IO.
    size = 2
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))
    for part in encoder.iterencode([EVT_DEVICES_RESPONSE, response]):
        size += len(part)
        if size > CLOUD_PACKET_BYTES:
            body = {"ok": False, "error": "cloud_response_too_large",
                    "max_packet_bytes": CLOUD_PACKET_BYTES}
            if data.get("method") == "POST" and data.get("path") == "/commands":
                body["outcome_unknown"] = True
            return {"request_id": data.get("request_id"), "payload": {"status": 413, "body": body}}
    return response
