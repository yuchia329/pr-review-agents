"""A witness of every request the workers send the model: a proxy in front of the endpoint.

A measurement tool, not part of the system. It runs as its own process, outside the workers, so
that what it writes survives a worker killed in the middle of a request:

    uv run python tools/model_witness.py --upstream http://HOST:8000/v1 \\
        --port 18000 --log DIRECTORY/witness.jsonl

The workers are then given `http://127.0.0.1:18000/v1` as their endpoint. Each request is
passed on as it came, headers and all, and its answer passed back as it came. Two lines are
written for each, as JSON, each flushed to disk before the request goes on:

- `{"event": "start", ...}` when the request has been read: `request` (a number counted from
  1), `at` (seconds since the epoch), `pid` (the process that sent it, found from its
  connection), `path`, `bytes`, and `sha256`, a digest of the body, so a request sent twice
  is seen as the same;
- `{"event": "end", ...}` when the answer has been passed back, or could not be: `request`,
  `at`, `status` (the endpoint's HTTP status, or null when it gave none), `bytes`,
  `delivered` (whether the sender was still there to take the answer), and `error`.

Nothing of a request's headers is written, so no key is. A request with a `start` and no
`end` was in flight when the witness itself stopped.
"""

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

PASSED_ON = ("authorization", "content-type", "accept")
"""The headers a request is passed on with. Others, such as `host`, are the proxy's own."""


class Witness:
    """Where each request is counted and written."""

    def __init__(self, upstream: str, log: Path) -> None:
        self.upstream = upstream.rstrip("/")
        self.client = httpx.Client(timeout=None)
        self._log = log.open("a")
        self._lock = threading.Lock()
        self._count = 0

    def numbered(self) -> int:
        with self._lock:
            self._count += 1
            return self._count

    def write(self, line: dict[str, Any]) -> None:
        with self._lock:
            self._log.write(json.dumps(line) + "\n")
            self._log.flush()
            os.fsync(self._log.fileno())


def sender(port: int) -> int | None:
    """The process on this machine whose connection comes from `port`, other than this one."""
    found = subprocess.run(
        ["lsof", "-nP", "-t", f"-iTCP:{port}"], capture_output=True, text=True, check=False
    )
    pids = [int(pid) for pid in found.stdout.split() if int(pid) != os.getpid()]
    return pids[0] if pids else None


def handler(witness: Witness) -> type[BaseHTTPRequestHandler]:
    class Passed(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            # One handler serves one connection, and a connection one sender.
            self.pid = sender(self.client_address[1])

        def do_GET(self) -> None:
            self._pass("GET", b"")

        def do_POST(self) -> None:
            self._pass("POST", self.rfile.read(int(self.headers.get("Content-Length", 0))))

        def _pass(self, method: str, body: bytes) -> None:
            number = witness.numbered()
            witness.write(
                {
                    "event": "start",
                    "request": number,
                    "at": time.time(),
                    "pid": self.pid,
                    "path": self.path,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            status: int | None = None
            answer = b""
            error: str | None = None
            delivered = False
            try:
                upstream = witness.client.request(
                    method,
                    witness.upstream + self.path.removeprefix("/v1"),
                    content=body,
                    headers={
                        name: value
                        for name, value in self.headers.items()
                        if name.lower() in PASSED_ON
                    },
                )
                status, answer = upstream.status_code, upstream.content
            except httpx.HTTPError as failed:
                error = f"{type(failed).__name__}: {failed}"
            try:
                if status is None:
                    self.send_error(502, "the endpoint could not be reached")
                else:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(answer)))
                    self.end_headers()
                    self.wfile.write(answer)
                    self.wfile.flush()
                delivered = True
            except OSError as gone:
                error = error or f"the sender was gone: {gone}"
                self.close_connection = True
            witness.write(
                {
                    "event": "end",
                    "request": number,
                    "at": time.time(),
                    "status": status,
                    "bytes": len(answer),
                    "delivered": delivered,
                    "error": error,
                }
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Passed


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--upstream", required=True, help="the endpoint, ending in /v1")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--log", type=Path, required=True)
    arguments = parser.parse_args()
    witness = Witness(arguments.upstream, arguments.log)
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), handler(witness))
    server.daemon_threads = True
    print(f"witnessing http://127.0.0.1:{arguments.port}/v1 for {witness.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
