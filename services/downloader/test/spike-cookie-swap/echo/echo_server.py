"""
Fake "Douyin" HTTPS endpoint for spike 2.

Terminates TLS with a cert whose CN/SAN is www.douyin.com (see
setup-certs.sh), so the real parser's httpx client -- which trusts the CA we
appended into its certifi bundle at container start -- connects to it
believing it is talking to the real domain. Every request's Cookie header is
recorded to /data/captured.log as one JSON object per line, then a
deliberately-not-real-Douyin-shaped body is returned. The parser is expected
to fail parsing that body and return its generic 400 envelope -- that is
correct and expected; the only thing under test is what arrived in the
Cookie header, not the response.

ThreadingHTTPServer (one thread per connection) is used deliberately so the
five concurrent requests fired by the jest spike spec are actually served
concurrently, not serialized -- serializing them would defeat the point of
the concurrency test.

Hold mechanism (step 3 "genuine overlap" evidence): the parser forwards the
client-supplied `aweme_id` verbatim as a query param on its outgoing request
(crawlers/douyin/web/web_crawler.py PostDetail -> urlencode). The jest spec
exploits that by suffixing an aweme_id with `_hold<ms>` (e.g.
"concurrent-0_hold400"). This handler strips that suffix before recording
`aweme_id`, records the Cookie header immediately on receipt (so the
capture's `ts` reflects true arrival time, before any hold), THEN sleeps for
`<ms>` milliseconds before writing the HTTP response. The TCP connection --
and the caller's in-flight `fetch()` promise -- stays open for the whole
hold, which is what makes "still in flight when the swap fires" a provable
fact rather than an assumption about scheduling order.
"""

import json
import os
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CAPTURE_FILE = "/data/captured.log"
_write_lock = threading.Lock()
_seq = 0
_HOLD_SUFFIX_RE = re.compile(r"^(?P<base>.*)_hold(?P<ms>\d+)$")


class Handler(BaseHTTPRequestHandler):
    def _handle(self):
        global _seq
        cookie = self.headers.get("Cookie", "")
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        aweme_id_raw = (qs.get("aweme_id") or [""])[0]

        hold_ms = 0
        aweme_id = aweme_id_raw
        m = _HOLD_SUFFIX_RE.match(aweme_id_raw)
        if m:
            aweme_id = m.group("base")
            hold_ms = int(m.group("ms"))

        with _write_lock:
            _seq += 1
            record = {
                "seq": _seq,
                "ts": time.time(),
                "method": self.command,
                "path": parsed.path,
                "aweme_id": aweme_id,
                # "<absent>" (not "") when the header is missing at all, so a
                # genuinely-empty-string cookie (a real failure mode we test
                # for) is never confused with "header not sent".
                "cookie": cookie if "Cookie" in self.headers else "<absent>",
            }
            line = json.dumps(record) + "\n"
            # Single write() of one line, flushed and fsynced before we
            # release the lock, so by the time the caller's HTTP response
            # comes back, the corresponding capture line is durably on disk
            # for the host-side jest process to read via the bind mount.
            fd = os.open(CAPTURE_FILE, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)

        if hold_ms:
            # Deliberately outside the lock: holding the lock here would
            # serialize the very concurrency the hold is meant to prove.
            time.sleep(hold_ms / 1000)

        body = json.dumps(
            {
                "status_code": 4004,
                "status_msg": "spike-echo: fake response, not real douyin data",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile="/certs/leaf.pem", keyfile="/certs/leaf.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print("spike echo server listening on :443 (TLS, threaded)", flush=True)
    server.serve_forever()
