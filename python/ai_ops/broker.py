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
# OpenAI-shaped and Anthropic-shaped inference surfaces. Allowlisting only the
# first silently denied legitimate traffic (qwen3.8-max -> "/messages").
DEFAULT_ALLOWED_PATHS = ("/chat/completions", "/messages")
# GET is denied by default and opened per provider, because a GET surface is a
# read of the account, not inference. Measured need: the Grok CLI issues
# `GET /models` before it will run any inference at all, and denying it makes the
# provider unusable rather than merely restricted.
DEFAULT_ALLOWED_GET_PATHS: tuple[str, ...] = ()
MAX_BODY = 8 * 1024 * 1024


# Hop-by-hop headers must not be relayed (RFC 7230), plus Host/Content-Length
# which urllib recomputes for the upstream request.
_DROP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    # urllib does not transparently decode content-encoding, so ask for identity.
    "accept-encoding",
    # both auth headers are stripped: the broker re-adds exactly one, in the
    # scheme the upstream expects. x-api-key matters because an Anthropic-shaped
    # client (Claude Code) authenticates with it, not with Authorization -- so a
    # broker that only rewrote Authorization would forward the sandbox's
    # PLACEHOLDER x-api-key untouched and inject the real value in a header the
    # backend ignores.
    "authorization", "x-api-key",
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


def _forward_headers(incoming, auth_header: str, auth_value: str) -> dict:
    """Relay the client's headers, swapping in the real credential.

    Forwarding matters beyond politeness: the upstream sits behind a CDN that
    rejects requests lacking the client's normal headers (notably User-Agent).
    An earlier version sent only content-type/accept and was answered with a
    Cloudflare 403 access-denied page.

    `auth_header` is where the real credential goes -- "authorization" for
    OpenAI/xAI-shaped backends, "x-api-key" for Anthropic-shaped ones. Both
    incoming auth headers were dropped above, so exactly one leaves here and it
    carries the real value, never the sandbox's placeholder.
    """
    out = {}
    for key in incoming.keys():
        low = key.lower()
        if low in _DROP_HEADERS:
            continue
        value = incoming.get(key)
        if value is not None:
            out[low] = value
    out[auth_header] = auth_value
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
        if b.max_calls is not None and b.attempts >= b.max_calls:
            # Denied, not throttled: a runaway agent that is merely slowed down
            # still spends the budget, just later.
            self._deny(429, f"provider call ceiling reached ({b.max_calls} per job)")
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
        b.attempts += 1
        req = urllib.request.Request(
            b.upstream + _join_path(b.upstream, self.path),
            data=payload,
            method="POST",
            headers=_forward_headers(self.headers, b.auth_header, b.auth_value()),
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
        b = self.broker
        if not any(self.path.endswith(p) for p in b.allowed_get_paths):
            self._deny(403, f"GET not proxied: {self.path}")
            return
        if b.max_calls is not None and b.attempts >= b.max_calls:
            self._deny(429, f"provider call ceiling reached ({b.max_calls} per job)")
            return
        b.attempts += 1
        req = urllib.request.Request(
            b.upstream + _join_path(b.upstream, self.path),
            method="GET",
            headers=_forward_headers(self.headers, b.auth_header, b.auth_value()),
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
        max_calls: Optional[int] = None,
        allowed_paths: Optional[tuple[str, ...]] = None,
        allowed_get_paths: Optional[tuple[str, ...]] = None,
        auth_header: str = "authorization",
        auth_scheme: str = "Bearer",
    ) -> None:
        if not credential:
            raise Refuse("broker requires a credential")
        # Accepts a credentials.Credential or a bare string. The object form
        # carries its class and expiry for diagnostics; the header value is
        # fetched per request so a future refreshing credential needs no change
        # here.
        self.credential = credential
        self.upstream = upstream.rstrip("/")
        self.allowed_models = set(allowed_models or ())
        # A provider routes different models over different wire APIs: OpenAI
        # models use /chat/completions, Anthropic-shaped ones use /messages.
        # Allowlisting only the first silently denied legitimate traffic
        # (qwen3.8-max -> "broker: path not allowed: /messages"). These two are
        # the inference surfaces; everything else stays refused.
        # Per-provider now: chatgpt.com, api.x.ai and api.anthropic.com do not
        # share one inference path, so a single hardcoded tuple would either deny
        # legitimate traffic or be widened until it allowlists nothing.
        self.allowed_paths = tuple(allowed_paths or DEFAULT_ALLOWED_PATHS)
        self.allowed_get_paths = tuple(allowed_get_paths or DEFAULT_ALLOWED_GET_PATHS)
        # Where and how the real credential is presented upstream. Anthropic
        # wants `x-api-key: <token>` with no scheme; OpenAI/xAI want
        # `authorization: Bearer <token>`.
        self.auth_header = auth_header.lower()
        self.auth_scheme = auth_scheme
        self.timeout_s = timeout_s
        self.unix_socket = unix_socket
        self.forwarded = 0
        # A hard ceiling on brokered calls for ONE job. Earned by a reviewer that
        # looped 35 times looking for a diff it could not reach: the job timeout
        # was the only bound, and every one of those calls was billed. None means
        # no ceiling configured.
        self.max_calls = max_calls
        # ATTEMPTS, not forwards. `forwarded` means "a model answered" and
        # live.sh's retry rule depends on exactly that meaning: forwarded == 0
        # is how it tells infrastructure failure from a real defect. But a
        # runaway loop whose calls all fail upstream never increments forwarded,
        # so a ceiling counting forwards would never fire on the case that most
        # needs bounding. Count what leaves the controller.
        self.attempts = 0
        self.denials: list[str] = []
        self._srv: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def auth_value(self) -> str:
        """The credential value for the upstream auth header, resolved per request.

        Per request rather than once at construction so that a credential which
        learns to refresh itself needs no change in the request path -- and so a
        long job cannot keep using a value that has since expired.
        """
        cred = self.credential
        token = cred.token if hasattr(cred, "token") else str(cred)
        return f"{self.auth_scheme} {token}".strip() if self.auth_scheme else token

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
