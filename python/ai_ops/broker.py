"""Credential broker: keeps the provider credential OUT of the sandbox.

Stage 1 injected the credential into the sandbox environment, which meant a
hostile provider could read and exfiltrate it. Here the controller holds the
credential and the sandbox is handed only a loopback endpoint plus a placeholder
key. The broker attaches the real Authorization header on the way upstream, so
the provider can USE the credential for the duration of the job but can never
READ it.

Verified against OpenCode 1.18.18: setting provider.<id>.options.baseURL
redirects the built-in provider, and options.apiKey is sent as
`Authorization: Bearer <value>` -- which is why the sandbox's copy is a
placeholder and the broker overwrites that header.

Residual, unchanged by this module: the provider can still *use* the credential
while the job runs (it is proxying for it, by design). What it can no longer do
is keep it afterwards. Narrowing usage further is the job of allowed_paths and
allowed_model below, plus --unshare-net once the sandbox reaches the broker over
a bound socket rather than shared loopback.
"""
from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
from typing import Optional

from .errors import Refuse

DEFAULT_UPSTREAM = "https://opencode.ai/zen/go/v1"
MAX_BODY = 8 * 1024 * 1024


class _Handler(http.server.BaseHTTPRequestHandler):
    broker: "CredentialBroker"

    def _deny(self, code: int, msg: str) -> None:
        self.broker.denials.append(msg)
        body = json.dumps({"error": {"message": f"broker: {msg}"}}).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        b = self.broker
        # Only the chat-completions surface is proxied; anything else the
        # provider tries to reach through the broker is refused.
        if not any(self.path.endswith(p) for p in b.allowed_paths):
            self._deny(403, f"path not allowed: {self.path}")
            return
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY:
            self._deny(413, "request too large")
            return
        payload = self.rfile.read(length)
        if b.allowed_model:
            try:
                requested = json.loads(payload).get("model")
            except Exception:
                self._deny(400, "unparseable request body")
                return
            if requested != b.allowed_model:
                self._deny(403, f"model not allowed: {requested!r}")
                return
        req = urllib.request.Request(
            b.upstream + self.path,
            data=payload,
            method="POST",
            headers={
                "authorization": f"Bearer {b.credential}",
                "content-type": self.headers.get("content-type", "application/json"),
                "accept": self.headers.get("accept", "application/json"),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=b.timeout_s) as resp:
                data = resp.read()
                self.send_response(resp.status)
                if resp.headers.get("content-type"):
                    self.send_header("content-type", resp.headers["content-type"])
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                b.forwarded += 1
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            b.forwarded += 1
        except Exception as exc:
            self._deny(502, f"upstream error: {type(exc).__name__}")

    def do_GET(self) -> None:  # noqa: N802
        self._deny(403, "GET not proxied")

    def log_message(self, *args) -> None:
        # Never log request lines: they can carry prompt content.
        return


class CredentialBroker:
    """Loopback credential broker scoped to one job."""

    def __init__(
        self,
        credential: str,
        *,
        upstream: str = DEFAULT_UPSTREAM,
        allowed_model: Optional[str] = None,
        timeout_s: int = 300,
    ) -> None:
        if not credential:
            raise Refuse("broker requires a credential")
        self.credential = credential
        self.upstream = upstream.rstrip("/")
        self.allowed_model = allowed_model
        self.allowed_paths = ("/chat/completions",)
        self.timeout_s = timeout_s
        self.forwarded = 0
        self.denials: list[str] = []
        self._srv: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        if self._srv is None:
            raise Refuse("broker is not running")
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "CredentialBroker":
        handler = type("BoundHandler", (_Handler,), {"broker": self})
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        # Drop the secret from memory promptly rather than waiting for GC.
        self.credential = ""
