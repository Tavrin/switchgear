"""In-sandbox launcher: bridges loopback TCP to the broker's unix socket.

Runs INSIDE the sandbox as the entry command. It holds no secret and grants no
authority -- it exists only because the provider addresses its API over an HTTP
URL, which needs a TCP listener, while the only channel out of a
`--unshare-net` sandbox is a bind-mounted unix socket.

Unix domain sockets are filesystem objects, not network objects, so they keep
working across a network namespace. That is what lets the sandbox have NO
network while still reaching the controller-side credential broker.

    provider --> 127.0.0.1:<port>  (this relay, inside the netns)
                      |
                 unix socket (bind-mounted)
                      |
                 broker (controller side, holds the credential)
                      |
                 upstream API

stdout is left strictly alone: it carries the provider's event stream, which the
controller parses. All relay diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import os
import selectors
import socket
import socketserver
import subprocess
import sys
import threading


class _Handler(socketserver.BaseRequestHandler):
    socket_path = ""

    def handle(self) -> None:
        try:
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.connect(self.socket_path)
        except OSError as exc:
            print(f"relay: cannot reach broker socket: {exc}", file=sys.stderr)
            return
        down = self.request
        sel = selectors.DefaultSelector()
        sel.register(down, selectors.EVENT_READ, up)
        sel.register(up, selectors.EVENT_READ, down)
        try:
            while True:
                for key, _ in sel.select(timeout=300):
                    src, dst = key.fileobj, key.data
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
                else:
                    if not sel.get_map():
                        return
        except OSError:
            return
        finally:
            sel.close()
            try:
                up.close()
            except OSError:
                pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sandbox-relay")
    parser.add_argument("--socket", required=True, help="bind-mounted broker unix socket")
    parser.add_argument("--port", type=int, required=True, help="loopback port to listen on")
    ns, rest = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        print("relay: no provider command given", file=sys.stderr)
        return 2

    handler = type("BoundHandler", (_Handler,), {"socket_path": ns.socket})
    server = _Server(("127.0.0.1", ns.port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        proc = subprocess.run(rest)
        return proc.returncode
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
