#!/usr/bin/env python3
"""The output secret detector.

Two failure modes, and the second is the dangerous one: missing a real secret,
and crying wolf. A detector that fires on ordinary agent output trains everyone
to ignore it, at which point it is strictly worse than nothing. Most of this file
is therefore about what must NOT trip it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from ai_ops import secrets  # noqa: E402


class Detects(unittest.TestCase):
    def _patterns(self, text):
        return {f["pattern"] for f in secrets.scan(text, "t")}

    def test_vendor_key_prefixes(self):
        cases = {
            "sk-ant-api03-" + "a" * 40: "anthropic-key",
            "sk-" + "B" * 40: "openai-key",
            "xai-" + "c" * 40: "xai-key",
            "ghp_" + "d" * 36: "github-token",
            "xoxb-1234567890-abcdefghij": "slack-token",
            "AKIAIOSFODNN7EXAMPLE": "aws-access-key",
            "AIza" + "e" * 35: "google-api-key",
        }
        for text, want in cases.items():
            self.assertIn(want, self._patterns(f"the key is {text} ok"), text[:12])

    def test_anthropic_keys_are_not_reported_as_openai(self):
        """The generic sk- rule would otherwise claim every Anthropic key and
        hide which vendor actually leaked."""
        found = self._patterns("sk-ant-api03-" + "a" * 40)
        self.assertIn("anthropic-key", found)
        self.assertNotIn("openai-key", found)

    def test_jwt_and_pem(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
        self.assertIn("jwt", self._patterns(jwt))
        self.assertIn("private-key-pem",
                      self._patterns("-----BEGIN OPENSSH PRIVATE KEY-----"))

    def test_auth_headers(self):
        for line in ("Authorization: Bearer " + "a" * 40,
                     "x-api-key: " + "b" * 40,
                     'api_key="' + "c" * 40 + '"'):
            self.assertIn("auth-header", self._patterns(line), line[:20])

    def test_count_is_reported(self):
        found = secrets.scan("xai-" + "a" * 30 + " and xai-" + "b" * 30, "t")
        self.assertEqual(found[0]["count"], 2)


class DoesNotCryWolf(unittest.TestCase):
    def _patterns(self, text):
        return {f["pattern"] for f in secrets.scan(text, "t")}

    def test_the_rails_own_placeholder_never_trips(self):
        """The broker writes this INTO the sandbox by design, so if it tripped
        the detector every brokered job would report a secret and the signal
        would be worthless."""
        self.assertEqual(secrets.scan(secrets.PLACEHOLDER, "t"), [])
        self.assertEqual(
            secrets.scan(f'ANTHROPIC_AUTH_TOKEN={secrets.PLACEHOLDER}', "t"), [])
        self.assertEqual(
            secrets.scan(f'"apiKey": "{secrets.PLACEHOLDER}"', "t"), [])

    def test_ordinary_agent_output_is_clean(self):
        """Every one of these appears in real evidence from this repo's own
        dogfooding runs."""
        benign = [
            "commit 90d6d9b8f2a1c4e6d8b0a2c4e6f8a0b2c4d6e8f0",
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "job 12ec58a1-9d31-46de-bb5d-3601d77db80c finished",
            "reading src/components/NeoTokyoMap.tsx (4821 bytes)",
            "npm install --save-dev @types/node typescript",
            "Authorization: Bearer <token>",
            "api_key=REDACTED",
            "x-api-key: ***",
            "model=claude-haiku-4-5-20251001 cost=0.0490175",
            "https://api.anthropic.com/v1/messages",
            "def process_task(self, task_id: str) -> dict[str, Any]:",
            "-----BEGIN CERTIFICATE-----",
        ]
        for text in benign:
            self.assertEqual(secrets.scan(text, "t"), [], f"false positive on: {text}")

    def test_a_whole_real_evidence_stream_is_clean(self):
        """The strongest available anti-false-positive check: run the detector
        over the captured provider streams this repo already commits."""
        for name in ("opencode-real-scout.jsonl", "grok-real-scout.jsonl",
                     "claude-real-scout.jsonl", "codex-real-scout.jsonl"):
            path = ROOT / "tests" / "fixtures" / name
            if not path.exists():
                continue
            found = secrets.scan(path.read_text(encoding="utf-8", errors="replace"), name)
            self.assertEqual(found, [], f"false positive in real stream {name}: {found}")


class NeverLeaksOrDestroys(unittest.TestCase):
    def test_the_finding_never_contains_the_secret(self):
        """A finding that quoted the value would copy it into result.json --
        smaller, more portable, and likelier to be pasted somewhere than the
        evidence file it came from."""
        secret = "xai-" + "z" * 40
        found = secrets.scan(f"token {secret}", "evidence/events.jsonl")
        self.assertTrue(found)
        blob = repr(found) + secrets.summarize(found)
        self.assertNotIn(secret, blob)
        self.assertNotIn("z" * 20, blob)

    def test_summary_names_where_and_what_to_do(self):
        found = secrets.scan("sk-" + "a" * 40, "evidence/stderr")
        msg = secrets.summarize(found)
        self.assertIn("evidence/stderr", msg)
        self.assertIn("openai-key", msg)
        self.assertIn("rotate", msg)
        self.assertIn("byte-intact", msg)

    def test_no_findings_means_no_message(self):
        self.assertEqual(secrets.summarize([]), "")

    def test_unreadable_files_are_skipped_not_raised(self):
        """This runs after the job is already paid for; it must never be a new
        way to lose a completed result."""
        self.assertEqual(secrets.scan_files({"gone": "/nonexistent/path/x"}), [])

    def test_empty_input(self):
        self.assertEqual(secrets.scan("", "t"), [])
        self.assertEqual(secrets.scan(None, "t"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
