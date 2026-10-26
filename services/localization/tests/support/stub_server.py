"""A real HTTP server on 127.0.0.1 (ephemeral port) that the real provider classes
talk to. Scenarios are plain data: status, delay, and whether the error body
echoes the request's Authorization header, the way a careless gateway does."""

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32

STT_BODY = {
    "language": "zh",
    "duration": 3.0,
    "segments": [{"id": 0, "start": 0.0, "end": 1.5, "text": " ni hao "}],
    "words": [
        {"word": "ni", "start": 0.0, "end": 0.7},
        {"word": "hao", "start": 0.7, "end": 1.5},
    ],
}


@dataclass
class Scenario:
    status: int = 200
    delay_seconds: float = 0.0
    echo_authorization: bool = False
    json_body: object | None = None
    raw_body: bytes | None = None
    # A non-200 answer that carries a perfectly valid body, so only the status
    # check can tell it from a success.
    body_with_status: object | None = None


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class StubServer:
    scenario: Scenario = field(default_factory=Scenario)
    requests: list[Recorded] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "StubServer":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence stderr
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                stub.requests.append(
                    Recorded(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                sc = stub.scenario
                if sc.delay_seconds:
                    time.sleep(sc.delay_seconds)
                if sc.status != 200:
                    payload = (
                        sc.body_with_status
                        if sc.body_with_status is not None
                        else {"error": "failed"}
                    )
                    if sc.echo_authorization and isinstance(payload, dict):
                        payload["echo"] = self.headers.get("Authorization", "")
                    self._send(sc.status, "application/json", json.dumps(payload).encode())
                    return
                if sc.raw_body is not None:
                    self._send(200, "application/octet-stream", sc.raw_body)
                elif sc.json_body is not None:
                    self._send(200, "application/json", json.dumps(sc.json_body).encode())
                elif self.path == "/v1/audio/speech":
                    self._send(200, "audio/wav", WAV)
                elif self.path == "/v1/audio/transcriptions":
                    self._send(200, "application/json", json.dumps(STT_BODY).encode())
                else:
                    self._send(404, "text/plain", b"no route")

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01), daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
