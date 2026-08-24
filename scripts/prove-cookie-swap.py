#!/usr/bin/env python
"""Prove the cookie swaps at runtime with NO process restart.

Plan phase 1 DoD: "change the Redis key -> the next Douyin request uses the new
cookie, without a restart". plan.md §7 repeats it for the whole project.

Why a RESP stub instead of a real redis-server: this exercises the real redis-py
client over a real TCP socket with `cookie_provider` completely unpatched --
`redis.Redis.from_url`, real connect/read timeouts, real protocol parsing. The
unit tests use fakeredis and monkeypatch `from_url`; this deliberately does not,
so it covers the one thing they cannot.

What this proves:   the cookie reaching the Douyin request headers changes
                    mid-process when the Redis value changes.
What it does NOT:   that Douyin accepts the cookie. That needs a live cookie and
                    a real request.

Run from the fork root:
    .venv/bin/python ../../scripts/prove-cookie-swap.py
"""
import os
import socket
import sys
import threading

HOST = "127.0.0.1"
KEY = b"douyin:cookie:current"

_value = b"COOKIE-BEFORE-swap-aaa111"
_lock = threading.Lock()


def set_value(v):
    global _value
    with _lock:
        _value = v


def _read_command(f):
    """Parse one RESP array command; returns list of bytes, or None on EOF."""
    line = f.readline()
    if not line:
        return None
    if not line.startswith(b"*"):
        return line.split()  # inline command
    parts = []
    for _ in range(int(line[1:].strip())):
        hdr = f.readline()
        if not hdr.startswith(b"$"):
            return None
        n = int(hdr[1:].strip())
        parts.append(f.read(n))
        f.read(2)  # trailing CRLF
    return parts


def _serve(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _bulk(b):
    return b"$%d\r\n%s\r\n" % (len(b), b)


# redis-py >= 5 opens the connection with `HELLO 3` and expects a RESP3 map
# reply. Answering it properly (rather than erroring) keeps the client on the
# same protocol it will use in production.
_HELLO_REPLY = (
    b"%7\r\n"
    + _bulk(b"server") + _bulk(b"redis")
    + _bulk(b"version") + _bulk(b"7.0.0")
    + _bulk(b"proto") + b":3\r\n"
    + _bulk(b"id") + b":1\r\n"
    + _bulk(b"mode") + _bulk(b"standalone")
    + _bulk(b"role") + _bulk(b"master")
    + _bulk(b"modules") + b"*0\r\n"
)


def _handle(conn):
    with conn, conn.makefile("rwb") as f:
        while True:
            try:
                cmd = _read_command(f)
            except (ValueError, OSError):
                return
            if not cmd:
                return
            name = cmd[0].upper()
            if name == b"GET":
                with _lock:
                    val = _value if cmd[1] == KEY else None
                f.write(b"$-1\r\n" if val is None else _bulk(val))
            elif name == b"HELLO":
                f.write(_HELLO_REPLY)
            else:
                f.write(b"+OK\r\n")  # CLIENT SETINFO / PING etc.
            f.flush()


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    threading.Thread(target=_serve, args=(srv,), daemon=True).start()

    os.environ["REDIS_URL"] = f"redis://{HOST}:{port}/0"
    os.environ["DOUYIN_COOKIE_FALLBACK"] = "FALLBACK-should-not-appear"
    print(f"RESP stub listening on {HOST}:{port}")

    import asyncio

    # This script lives outside the fork, so sys.path[0] is scripts/. The fork
    # root must be importable -- run this from the fork root.
    sys.path.insert(0, os.getcwd())
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    crawler = DouyinWebCrawler()

    first = asyncio.run(crawler.get_douyin_headers())["headers"]["Cookie"]
    print(f"  call 1 -> {first}")

    # Someone sets the Redis key. No restart, no re-import, same crawler object.
    set_value(b"COOKIE-AFTER-swap-bbb222")

    second = asyncio.run(crawler.get_douyin_headers())["headers"]["Cookie"]
    print(f"  call 2 -> {second}")

    ok = True
    if first != "COOKIE-BEFORE-swap-aaa111":
        print("FAIL: call 1 did not read the Redis value"); ok = False
    if second != "COOKIE-AFTER-swap-bbb222":
        print("FAIL: call 2 did not pick up the new Redis value"); ok = False
    if first == second:
        print("FAIL: cookie did not change"); ok = False
    if "FALLBACK" in first or "FALLBACK" in second:
        print("FAIL: fell back to env despite a reachable Redis"); ok = False

    print("\nPASS: cookie changed mid-process, no restart." if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
