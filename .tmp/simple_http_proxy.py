"""Simple HTTP/HTTPS CONNECT proxy for sharing internet with SA-02m device."""
import socket
import threading
import select
import sys


def transfer(src, dst):
    try:
        while True:
            r, _, _ = select.select([src], [], [], 60)
            if not r:
                break
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass


def handle_client(client):
    try:
        request = b""
        client.settimeout(10)
        while b"\r\n\r\n" not in request:
            chunk = client.recv(4096)
            if not chunk:
                return
            request += chunk

        lines = request.split(b"\r\n")
        first_line = lines[0].decode(errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) < 2:
            return
        method, url = parts[0], parts[1]

        if method == "CONNECT":
            host, port = url.rsplit(":", 1)
            port = int(port)
            server = socket.create_connection((host, port), timeout=15)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or 80
            server = socket.create_connection((host, port), timeout=15)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            new_req = request.replace(url.encode(), path.encode(), 1)
            server.sendall(new_req)

        client.settimeout(None)
        server.settimeout(None)
        t1 = threading.Thread(target=transfer, args=(client, server), daemon=True)
        t2 = threading.Thread(target=transfer, args=(server, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as e:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    port = 8899
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(50)
    print(f"HTTP proxy listening on 0.0.0.0:{port}", flush=True)
    while True:
        try:
            client, addr = srv.accept()
            t = threading.Thread(target=handle_client, args=(client,), daemon=True)
            t.start()
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
