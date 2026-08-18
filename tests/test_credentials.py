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
        self.assertIn("re-login", msg)

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
            self.assertEqual(bk.authorization(), "Bearer PLAIN")
        cred = creds.Credential(token="TOK", cls="oauth", source="/x")
        with CredentialBroker(cred, upstream="http://127.0.0.1:9/v1") as bk:
            self.assertEqual(bk.authorization(), "Bearer TOK")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
