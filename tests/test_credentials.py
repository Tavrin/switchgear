#!/usr/bin/env python3
"""Credential classes, and the one invariant that must never bend.

Three of the four providers this rail targets authenticate with OAuth sessions
rather than API keys. Every extractor below was written against a real file on
this machine (structure inspected, values never read), because guessing a
credential layout fails in the one direction that matters.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from ai_ops import credentials as creds  # noqa: E402
from ai_ops.errors import Refuse  # noqa: E402

# A recognisable stand-in. If this string ever appears in anything the module
# hands back, a refresh token has leaked into a place it must never reach.
REFRESH = "REFRESH-TOKEN-MUST-NEVER-BE-READ"


def _write(path: Path, obj: dict) -> str:
    path.write_text(json.dumps(obj))
    os.chmod(path, 0o600)
    return str(path)


def _jwt(exp: float) -> str:
    import base64

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


class RefreshTokenIsNeverRead(unittest.TestCase):
    """The invariant, stated as three tests rather than a comment.

    An access token expires in about an hour; a refresh token is the whole
    subscription. The cheapest way to guarantee it cannot leak into a header, a
    log or a sandbox is to never load it into memory at all.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-cred-"))

    def _assert_absent(self, cred):
        blob = json.dumps(
            {"token": cred.token, "source": cred.source, "cls": cred.cls,
             "authorization": cred.authorization(), "describe": cred.describe()}
        )
        self.assertNotIn(REFRESH, blob)

    def test_grok(self):
        path = _write(self.tmp / "grok.json", {
            "https://auth.x.ai::abc": {
                "key": "ACCESS-GROK", "auth_mode": "oidc",
                "refresh_token": REFRESH,
                "expires_at": "2099-08-18T07:46:34.587567321Z",
                "oidc_issuer": "https://auth.x.ai",
            }
        })
        cred = creds.load_oauth_credential(
            "grok", {"auth_file": path, "auth_format": "grok-oidc"}
        )
        self.assertEqual(cred.token, "ACCESS-GROK")
        self.assertEqual(cred.authorization(), "Bearer ACCESS-GROK")
        self._assert_absent(cred)

    def test_codex(self):
        path = _write(self.tmp / "codex.json", {
            "OPENAI_API_KEY": None,
            "tokens": {"access_token": _jwt(time.time() + 7200),
                       "refresh_token": REFRESH, "account_id": "acct"},
        })
        cred = creds.load_oauth_credential(
            "codex", {"auth_file": path, "auth_format": "codex-tokens"}
        )
        self.assertTrue(cred.token)
        self._assert_absent(cred)

    def test_claude(self):
        path = _write(self.tmp / "claude.json", {
            "claudeAiOauth": {
                "accessToken": "ACCESS-CLAUDE", "refreshToken": REFRESH,
                # MILLISECONDS -- measured. Reading it as seconds puts expiry in
                # 1970 and refuses every job.
                "expiresAt": int((time.time() + 7200) * 1000),
                "subscriptionType": "max",
            }
        })
        cred = creds.load_oauth_credential(
            "claude", {"auth_file": path, "auth_format": "claude-oauth"}
        )
        self.assertEqual(cred.token, "ACCESS-CLAUDE")
        self._assert_absent(cred)


class ExpiryHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-exp-"))

    def test_claude_expiry_is_milliseconds_not_seconds(self):
        """The whole-file regression this guards: treating the ms stamp as
        seconds dates it to 1970 and refuses every job forever."""
        path = _write(self.tmp / "c.json", {"claudeAiOauth": {
            "accessToken": "A", "refreshToken": REFRESH,
            "expiresAt": int((time.time() + 3600) * 1000)}})
        cred = creds.load_oauth_credential(
            "claude", {"auth_file": path, "auth_format": "claude-oauth"})
        remaining = cred.seconds_remaining()
        self.assertGreater(remaining, 3000)
        self.assertLess(remaining, 4000)

    def test_an_expired_session_refuses_with_an_actionable_message(self):
        path = _write(self.tmp / "g.json", {"s::1": {
            "key": "OLD", "refresh_token": REFRESH,
            "expires_at": "2020-01-01T00:00:00.000000000Z"}})
        with self.assertRaises(Refuse) as cm:
            creds.load_oauth_credential(
                "grok", {"auth_file": path, "auth_format": "grok-oidc"})
        msg = str(cm.exception)
        self.assertIn("expired", msg)
        # Actionable: it must say how to fix it, not just that it is broken.
        self.assertTrue("refresh" in msg or "log in" in msg, msg)

    def test_a_token_expiring_within_the_skew_is_already_expired(self):
        """A job starting with 20 seconds left outlives its own credential
        mid-flight; a clear refusal now beats a 401 from inside the sandbox."""
        soon = time.time() + (creds.EXPIRY_SKEW_S // 2)
        path = _write(self.tmp / "s.json", {"claudeAiOauth": {
            "accessToken": "A", "expiresAt": int(soon * 1000)}})
        with self.assertRaises(Refuse):
            creds.load_oauth_credential(
                "claude", {"auth_file": path, "auth_format": "claude-oauth"})

    def test_nanosecond_iso_stamps_parse(self):
        """Grok and Codex write 9 fractional digits with a Z suffix, which
        datetime.fromisoformat does not accept."""
        self.assertIsNotNone(creds._iso_to_epoch("2026-08-18T07:46:34.587567321Z"))
        self.assertIsNotNone(creds._iso_to_epoch("2026-08-18T07:46:34Z"))
        self.assertIsNone(creds._iso_to_epoch("not-a-date"))


class Refusals(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-ref-"))

    def test_a_world_readable_session_file_refuses(self):
        path = self.tmp / "w.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "A"}}))
        os.chmod(path, 0o644)
        with self.assertRaises(Refuse) as cm:
            creds.load_oauth_credential(
                "claude", {"auth_file": str(path), "auth_format": "claude-oauth"})
        self.assertIn("chmod 600", str(cm.exception))

    def test_a_missing_session_says_to_log_in(self):
        with self.assertRaises(Refuse) as cm:
            creds.load_oauth_credential(
                "grok", {"auth_file": str(self.tmp / "nope.json"),
                         "auth_format": "grok-oidc"})
        self.assertIn("log in", str(cm.exception))

    def test_unknown_class_and_format_refuse_rather_than_defaulting(self):
        with self.assertRaises(Refuse):
            creds.load_credential("x", {"credential_class": "magic"})
        with self.assertRaises(Refuse):
            creds.load_oauth_credential(
                "x", {"auth_file": "/tmp/x", "auth_format": "invented"})

    def test_oauth_without_registry_wiring_refuses(self):
        with self.assertRaises(Refuse) as cm:
            creds.load_credential("x", {"credential_class": "oauth"})
        self.assertIn("auth_file", str(cm.exception))

    def test_api_key_class_is_still_the_default(self):
        """Existing providers must not change behaviour because OAuth arrived."""
        os.environ["AI_OPS_PROVIDER_CREDENTIAL_FILE"] = str(self.tmp / "absent")
        try:
            self.assertIsNone(creds.load_credential("opencode-go", {}))
        finally:
            os.environ.pop("AI_OPS_PROVIDER_CREDENTIAL_FILE", None)


class BrokerUsesTheCredential(unittest.TestCase):
    def test_broker_accepts_a_credential_object_and_a_bare_string(self):
        from ai_ops.broker import CredentialBroker

        with CredentialBroker("PLAIN", upstream="http://127.0.0.1:9/v1") as bk:
            self.assertEqual(bk.auth_value(), "Bearer PLAIN")
        cred = creds.Credential(token="TOK", cls="oauth", source="/x")
        with CredentialBroker(cred, upstream="http://127.0.0.1:9/v1") as bk:
            self.assertEqual(bk.auth_value(), "Bearer TOK")

    def test_anthropic_shaped_provider_swaps_x_api_key_not_authorization(self):
        """Measured: Claude Code authenticates with `x-api-key`, not Authorization.

        A broker that only rewrote Authorization would forward the sandbox's
        PLACEHOLDER x-api-key to the backend and inject the real credential into
        a header the backend ignores -- i.e. leak the placeholder AND fail auth.
        This drives a recording upstream and checks the header the real value
        lands in, with no spend.
        """
        import http.server
        import threading
        import urllib.request

        seen = {}

        class Up(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                self.rfile.read(n)
                seen["x-api-key"] = self.headers.get("x-api-key")
                seen["authorization"] = self.headers.get("authorization")
                body = b"{}"
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Up)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        upstream = f"http://127.0.0.1:{up.server_address[1]}/v1"

        from ai_ops.broker import CredentialBroker

        with CredentialBroker(
            "REAL-ANTHROPIC-TOKEN", upstream=upstream,
            allowed_paths=("/messages",),
            auth_header="x-api-key", auth_scheme="",
        ) as bk:
            req = urllib.request.Request(
                bk.base_url + "/v1/messages",
                data=b'{"model":"m"}',
                # The sandbox's placeholder, in the header a real Claude sends.
                headers={"content-type": "application/json",
                         "x-api-key": "broker-placeholder-not-a-credential"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
        up.shutdown()

        # Real token in x-api-key, no scheme prefix; the placeholder is gone; and
        # it did NOT leak into Authorization either.
        self.assertEqual(seen["x-api-key"], "REAL-ANTHROPIC-TOKEN")
        self.assertIsNone(seen["authorization"])

    def test_path_allowlist_ignores_the_query_string(self):
        """Measured: Claude Code posts to `/v1/messages?beta=true`.

        An allowlist written the obvious way (endswith "/v1/messages") is False
        for that, so every request from an otherwise-correct provider would be
        denied -- fail-closed for a reason nobody could see. The matcher compares
        the path component only.
        """
        from ai_ops.broker import _path_allowed

        self.assertTrue(_path_allowed("/v1/messages?beta=true", ("/v1/messages",)))
        self.assertTrue(_path_allowed("/v1/messages", ("/v1/messages",)))
        self.assertTrue(_path_allowed("/v1/chat/completions?x=1", ("/chat/completions",)))
        # Still a real allowlist: a query string cannot be used to sneak past it.
        self.assertFalse(_path_allowed("/v1/admin?path=/v1/messages", ("/v1/messages",)))
        self.assertFalse(_path_allowed("/v1/embeddings", ("/v1/messages",)))

    def test_get_is_denied_unless_a_provider_opens_it(self):
        """A GET surface is a read of the account, not inference, so it stays
        shut by default. Grok needs GET /models before it will infer at all."""
        import urllib.error
        import urllib.request

        from ai_ops.broker import CredentialBroker

        def get(bk, path):
            try:
                urllib.request.urlopen(bk.base_url + path, timeout=5)
                return 200
            except urllib.error.HTTPError as exc:
                return exc.code
            except Exception:
                return 0

        with CredentialBroker("S", upstream="http://127.0.0.1:9/v1") as bk:
            self.assertEqual(get(bk, "/v1/models"), 403)
        with CredentialBroker("S", upstream="http://127.0.0.1:9/v1",
                              allowed_get_paths=("/models",)) as bk:
            # Opened: it now reaches the (dead) upstream instead of being refused.
            self.assertEqual(get(bk, "/v1/models"), 502)
            self.assertEqual(get(bk, "/v1/account"), 403)


class FallbackTierSandboxCredential(unittest.TestCase):
    """Grok validates its session locally, so its ACCESS token goes in the
    sandbox -- but the refresh token must not, and OpenCode must never get one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-fb-"))

    def test_strip_refresh_tokens_removes_them_at_any_depth(self):
        doc = {"a": {"key": "K", "refresh_token": REFRESH},
               "b": [{"refreshToken": REFRESH, "x": 1}],
               "nested": {"deep": {"my_refresh_thing": REFRESH, "keep": "ok"}}}
        out = creds.strip_refresh_tokens(doc)
        self.assertNotIn(REFRESH, json.dumps(out))
        self.assertEqual(out["a"]["key"], "K")
        self.assertEqual(out["b"][0]["x"], 1)
        self.assertEqual(out["nested"]["deep"]["keep"], "ok")

    def test_load_sandbox_session_strips_refresh_and_keeps_the_rest(self):
        path = _write(self.tmp / "grok.json", {
            "https://auth.x.ai::abc": {
                "key": "ACCESS", "auth_mode": "oidc", "refresh_token": REFRESH,
                "oidc_issuer": "https://auth.x.ai",
                "expires_at": "2099-01-01T00:00:00.000000000Z"}})
        session = creds.load_sandbox_session(
            "grok", {"auth_file": path, "auth_format": "grok-oidc"})
        blob = json.dumps(session)
        self.assertNotIn(REFRESH, blob)          # refresh gone
        self.assertIn("ACCESS", blob)            # access token kept
        self.assertIn("oidc_issuer", blob)       # structure Grok validates kept

    def test_an_expired_session_refuses_before_it_is_written(self):
        path = _write(self.tmp / "old.json", {"s::1": {
            "key": "OLD", "refresh_token": REFRESH,
            "expires_at": "2020-01-01T00:00:00.000000000Z"}})
        with self.assertRaises(Refuse):
            creds.load_sandbox_session("grok", {"auth_file": path, "auth_format": "grok-oidc"})

    def test_grok_adapter_writes_a_private_refresh_free_session(self):
        from ai_ops.adapters import get_adapter

        src = _write(self.tmp / "grok.json", {
            "https://auth.x.ai::abc": {
                "key": "ACCESS", "refresh_token": REFRESH,
                "oidc_issuer": "https://auth.x.ai",
                "expires_at": "2099-01-01T00:00:00.000000000Z"}})
        home = str(self.tmp / "home")
        os.makedirs(home)
        dest = get_adapter("grok").write_sandbox_credential(
            home, "grok", {"auth_file": src, "auth_format": "grok-oidc"})
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o600)
        written = Path(dest).read_text()
        self.assertNotIn(REFRESH, written)
        self.assertIn("ACCESS", written)

    def test_grok_is_fallback_tier_and_opencode_is_not(self):
        from ai_ops.adapters import get_adapter

        self.assertTrue(getattr(get_adapter("grok"), "credential_in_sandbox", False))
        self.assertFalse(getattr(get_adapter("opencode"), "credential_in_sandbox", False))

    def test_grok_env_does_not_set_a_placeholder_token(self):
        """The session file provides auth; a placeholder token env would override
        it and fail Grok's local validation (measured)."""
        from ai_ops.adapters import get_adapter

        env = get_adapter("grok").isolation_env(
            str(self.tmp / "h2"), {}, "http://127.0.0.1:8099")
        self.assertIn("GROK_CLI_BASE_URL", env)
        self.assertNotIn("GROK_AUTH_PROVIDER_ACCESS_TOKEN", env)


class ProviderPinning(unittest.TestCase):
    """A second real binary must not be mistaken for a committed mock."""

    def test_every_pinned_binary_is_recognised_as_live(self):
        from ai_ops.compat import PINNED_PROVIDERS, pinned_for_path

        for name, rec in PINNED_PROVIDERS.items():
            path = rec["path"]
            if not os.path.exists(path):
                continue  # not installed on this machine; nothing to assert
            match = pinned_for_path(path)
            self.assertIsNotNone(match, f"{name} not recognised at {path}")
            self.assertEqual(match[0], name)

    def test_a_symlinked_launcher_resolves_to_the_same_identity(self):
        """~/.grok/bin/grok is a symlink into ~/.grok/downloads. Comparing raw
        paths would classify the launcher as an unpinned binary, i.e. a mock."""
        from ai_ops.compat import pinned_for_path

        link = os.path.expanduser("~/.grok/bin/grok")
        if not os.path.exists(link):
            self.skipTest("grok not installed")
        # Grok self-updates and repoints this symlink (1.0.4 -> 1.0.5 was
        # observed mid-session). It must still be RECOGNISED as grok -- an
        # unrecognised real binary falls through to the committed-mock branch and
        # would run with neither the live gate nor a broker. The version
        # assertion is what refuses an untested build, loudly.
        self.assertEqual(pinned_for_path(link)[0], "grok")

    def test_a_pinned_binary_still_needs_the_live_gate(self):
        """The wart this replaced: resolve_provider compared against the ONE
        OpenCode pin, so any other real agent CLI classified as a mock and ran
        with neither the live gate nor a broker."""
        from ai_ops.errors import Refuse
        from ai_ops.provider import resolve_provider

        path = "/home/user/.grok/downloads/grok-linux-x86_64"
        if not os.path.exists(path):
            self.skipTest("grok not installed")
        old = os.environ.pop("AI_OPS_ALLOW_LIVE_PROVIDER", None)
        try:
            with self.assertRaises(Refuse) as cm:
                resolve_provider(path)
            self.assertIn("grok", str(cm.exception))
        finally:
            if old is not None:
                os.environ["AI_OPS_ALLOW_LIVE_PROVIDER"] = old

    def test_version_pins_are_per_provider_and_substring_matched(self):
        """OpenCode prints a bare `1.18.18`; Grok prints
        `grok 1.0.4 (d846eb93d9) [stable]`. Equality would reject Grok outright."""
        from ai_ops.errors import Refuse
        from ai_ops.provider import assert_pinned_version

        self.assertTrue(assert_pinned_version(0, b"1.18.18", False, "opencode"))
        self.assertTrue(
            assert_pinned_version(0, b"grok 1.0.4 (d846eb93d9) [stable]", False, "grok")
        )
        with self.assertRaises(Refuse):
            assert_pinned_version(0, b"grok 9.9.9", False, "grok")

    def test_an_unpinned_provider_refuses_rather_than_passing(self):
        """An absent pin must not read as 'no constraint'."""
        from ai_ops.errors import Refuse
        from ai_ops.provider import assert_pinned_version

        # A provider name that is deliberately NOT in PINNED_PROVIDERS. (This
        # used to use "codex", which then got pinned -- the assertion is about
        # the absent-pin rule, not about any particular provider.)
        with self.assertRaises(Refuse) as cm:
            assert_pinned_version(0, b"anything", False, "not-a-pinned-provider")
        self.assertIn("no pinned version", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
