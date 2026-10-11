"""Local HTTP server for the output tests.

With port binding: http.server on 127.0.0.1. Without port binding: the same
handler class on one end of a socketpair (outputs.CONNECTION_FACTORY).
"""
import email.parser
import email.policy
import http.client
import http.server
import socket
import threading

import support  # noqa: F401
from netsim import binding_allowed
from ix1300 import outputs


class Recorder:
    def __init__(self, status=200, body=b'"6f1c0f8e-task"'):
        self.requests = []
        self.status = status
        self.body = body


def _handler(rec):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n)
            rec.requests.append(dict(path=self.path, headers=dict(self.headers.items()), body=data))
            self.send_response(rec.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(rec.body)))
            self.end_headers()
            try:
                self.wfile.write(rec.body)
            except (BrokenPipeError, ConnectionResetError):
                pass                         # the client reads only a part of a long reply

        def log_message(self, *a):
            pass
    return H


def _serve_one(handler, sk):
    try:
        handler(sk, ("127.0.0.1", 1), None)
    finally:
        sk.close()


class FakeServer:
    """Context manager. Gives .url and .rec (the received requests)."""

    def __init__(self, status=200):
        self.rec = Recorder(status)
        self.httpd = None

    def __enter__(self):
        H = _handler(self.rec)
        if binding_allowed():
            self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        else:
            class Conn(http.client.HTTPConnection):
                def connect(self):
                    a, b = socket.socketpair()
                    self.sock = a
                    threading.Thread(target=_serve_one, args=(H, b), daemon=True).start()

            outputs.CONNECTION_FACTORY = lambda scheme, host, port, timeout, ctx: Conn(host, port, timeout=timeout)
            self.url = "http://fake.invalid:8000"
        return self

    def __exit__(self, *exc):
        outputs.CONNECTION_FACTORY = None
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


def parse_multipart(req):
    """Return a list of (name, filename, content type, bytes)."""
    ctype = req["headers"]["Content-Type"]
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req["body"])
    parts = []
    for part in msg.iter_parts():
        parts.append((part.get_param("name", header="content-disposition"), part.get_filename(),
                      part.get_content_type(), part.get_payload(decode=True)))
    return parts
