"""Cloud-control's private root-peer socket hop; transport IDs never dedupe IDs."""
import http.client
import json
import socket

from .contract import APIError, MAX_RESPONSE, encoded, validate_request

SOCKET_PATH = "/run/sa02m-home/api.sock"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path=SOCKET_PATH, timeout=8):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def request(data, connection_factory=UnixConnection):
    try:
        if not isinstance(data, dict):
            raise APIError(400, "invalid_request")
        method, path = data.get("method"), data.get("path")
        validate_request(method, path, data.get("query"), data.get("body"))
        envelope = {k: data.get(k) for k in ("method", "path", "query", "body")}
        connection = connection_factory()
        try:
            connection.request("POST", "/internal/home/v1", encoded(envelope), {"Content-Type": "application/json"})
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
        return {"status": exc.status, "body": {"ok": False, "error": exc.code}}
    except (OSError, ValueError, http.client.HTTPException):
        return {"status": 503, "body": {"ok": False, "error": "home_api_unavailable"}}
