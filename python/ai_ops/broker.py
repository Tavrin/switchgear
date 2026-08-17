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
import os
import socket
import socketserver
import threading
import urllib.error
import urllib.request
from typing import Optional

from .errors import Refuse

DEFAULT_UPSTREAM = "https://opencode.ai/zen/go/v1"
MAX_BODY = 8 * 1024 * 1024


# Hop-by-hop headers must not be relayed (RFC 7230), plus Host/Content-Length
# which urllib recomputes for the upstream request.
_DROP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    # urllib does not transparently decode content-encoding, so ask for identity.
    "accept-encoding",
    # replaced with the real credential
    "authorization",
})


def _join_path(upstream: str, path: str) -> str:
    """Avoid duplicating the API version segment.

    Clients may address the broker with or without a leading /v1 depending on how
    baseURL was written; the upstream already ends in /v1. Collapse the overlap
    rather than trusting one convention.
    """
    if upstream.rstrip("/").endswith("/v1") and path.startswith("/v1/"):
        return path[3:]
    return path


def _forward_headers(incoming, credential: str) -> dict:
    """Relay the client's headers, swapping in the real credential.

    Forwarding matters beyond politeness: the upstream sits behind a CDN that
    rejects requests lacking the client's normal headers (notably User-Agent).
    An earlier version sent only content-type/accept and was answered with a
    Cloudflare 403 access-denied page.
    """
    out = {}
    for key in incoming.keys():
        low = key.lower()
        if low in _DROP_HEADERS:
            continue
        value = incoming.get(key)
        if value is not None:
            out[low] = value
    out["authorization"] = f"Bearer {credential}"
    out.setdefault("content-type", "application/json")
    out.setdefault("accept", "application/json")
    return out


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
        if b.allowed_models:
            try:
                requested = json.loads(payload).get("model")
            except Exception:
                self._deny(400, "unparseable request body")
                return
            if requested not in b.allowed_models:
                self._deny(403, f"model not allowed: {requested!r}")
                return
        req = urllib.request.Request(
            b.upstream + _join_path(b.upstream, self.path),
            data=payload,
            method="POST",
            headers=_forward_headers(self.headers, b.credential),
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


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    """HTTP over a unix socket. BaseHTTPRequestHandler needs these attributes."""

    daemon_threads = True
    allow_reuse_address = True
    server_name = "broker"
    server_port = 0

    def get_request(self):
        req, _ = super().get_request()
        return req, ("127.0.0.1", 0)  # handler expects an addr pair


class CredentialBroker:
    """Loopback credential broker scoped to one job."""

    def __init__(
        self,
        credential: str,
        *,
        upstream: str = DEFAULT_UPSTREAM,
        allowed_models: Optional[set] = None,
        timeout_s: int = 300,
        unix_socket: Optional[str] = None,
    ) -> None:
        if not credential:
            raise Refuse("broker requires a credential")
        self.credential = credential
        self.upstream = upstream.rstrip("/")
        self.allowed_models = set(allowed_models or ())
        # A provider routes different models over different wire APIs: OpenAI
        # models use /chat/completions, Anthropic-shaped ones use /messages.
        # Allowlisting only the first silently denied legitimate traffic
        # (qwen3.8-max -> "broker: path not allowed: /messages"). These two are
        # the inference surfaces; everything else stays refused.
        self.allowed_paths = ("/chat/completions", "/messages")
        self.timeout_s = timeout_s
        self.unix_socket = unix_socket
        self.forwarded = 0
        self.denials: list[str] = []
        self._srv: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def base_url(self) -> str:
        if self._srv is None:
            raise Refuse("broker is not running")
        if self.unix_socket:
            raise Refuse("unix-socket broker is addressed through the in-sandbox relay")
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "CredentialBroker":
        handler = type("BoundHandler", (_Handler,), {"broker": self})
        if self.unix_socket:
            # Unix socket mode: the sandbox can then run --unshare-net and still
            # reach us, because unix sockets are filesystem objects and survive a
            # network namespace. Mode 600 -- only this user's processes.
            if os.path.exists(self.unix_socket):
                os.unlink(self.unix_socket)
            self._srv = _UnixHTTPServer(self.unix_socket, handler)
            os.chmod(self.unix_socket, 0o600)
        else:
            self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._srv is not None and self.unix_socket:
            try:
                os.unlink(self.unix_socket)
            except OSError:
                pass
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        # Drop the secret from memory promptly rather than waiting for GC.
        self.credential = ""
