"""Shared pytest fixtures.

Provides a ``stub_server`` fixture that runs a real stdlib HTTP server on an
ephemeral port. Tests enqueue responses per (method, path) and inspect hits
after the call.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest


class StubServer:
    def __init__(self) -> None:
        self._queue: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.hits: list[dict[str, Any]] = []
        self._httpd = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def enqueue(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        body_bytes = json.dumps(body).encode() if body is not None else b""
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        self._queue.setdefault((method.upper(), path), []).append(
            {"status": status, "body": body_bytes, "headers": hdrs},
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any, **kwargs: Any) -> None:
                return

            def _serve(self, method: str) -> None:
                path_only = self.path.split("?", 1)[0]
                length = int(self.headers.get("Content-Length") or 0)
                body_raw = self.rfile.read(length) if length else b""
                outer.hits.append(
                    {
                        "method": method,
                        "path": self.path,
                        "path_only": path_only,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body_raw,
                    },
                )
                queue = outer._queue.get((method, path_only))
                if not queue:
                    msg = f"no response queued for {method} {path_only}"
                    self.send_response(599)
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg.encode())
                    return
                resp = queue.pop(0)
                self.send_response(resp["status"])
                for k, v in resp["headers"].items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp["body"])))
                self.end_headers()
                self.wfile.write(resp["body"])

            def do_GET(self) -> None:  # noqa: N802  (stdlib contract)
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._serve("PATCH")

            def do_PUT(self) -> None:  # noqa: N802
                self._serve("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._serve("DELETE")

        return Handler


@pytest.fixture
def stub_server():
    srv = StubServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()
