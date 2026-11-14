"""Tiny local fixture server for autofill tests: 127.0.0.1 only, never the internet.

Serves tests/fixtures/forms/*, records every request (method, path), and
answers POST /submit with a 303 to /success.html ("Thank you for applying").
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FORMS = Path(__file__).resolve().parent / "fixtures" / "forms"
SUCCESS = b"<!doctype html><title>Done</title><h1>Thank you for applying!</h1>"


class FormServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8",
                      headers: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                server.requests.append(("GET", self.path))
                path = self.path.split("?", 1)[0]
                if path == "/success.html":
                    return self._send(200, SUCCESS)
                f = (FORMS / path.lstrip("/")).resolve()
                if FORMS in f.parents and f.is_file():
                    return self._send(200, f.read_bytes())
                return self._send(404, b"not found")

            def _post(self):
                server.requests.append((self.command, self.path))
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                return self._send(303, b"", headers={"Location": "/success.html"})

            do_POST = do_PUT = do_PATCH = do_DELETE = _post

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def url(self, name: str) -> str:
        return f"{self.base}/{name}"

    def posts(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    def gets(self, path: str) -> int:
        return sum(1 for m, p in self.requests if m == "GET" and p.split("?")[0] == path)

    def start(self) -> "FormServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
