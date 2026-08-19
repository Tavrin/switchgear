#!/usr/bin/env python3
"""Quota readings and the budget switchgear can actually enforce.

Two separate things on purpose. The subscription pools that publish a reading
(claude, codex) are NOT what this rail spends -- opencode-go publishes nothing --
so blending them into one "remaining" figure would invent a number. What the rail
can enforce is a ceiling over measured spend, plus a hard cap on provider calls
per job.
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

from switchgear import quota  # noqa: E402
from switchgear.errors import Refuse  # noqa: E402


class ExternalReadings(unittest.TestCase):
    """Both published shapes must reduce to one, or callers hand-roll it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-quota-"))
        self._real = quota.QUOTA_DIR
        quota.QUOTA_DIR = str(self.tmp)

    def tearDown(self):
        quota.QUOTA_DIR = self._real

    def _write(self, name, obj):
        (self.tmp / f"{name}.json").write_text(json.dumps(obj))

    def test_object_shaped_windows_are_normalized(self):
        self._write(
            "claude",
            {
                "provider": "claude",
                "captured_at": time.time(),
                "windows": {
                    "five_hour": {"used_percent": 15, "remaining_percent": 85, "resets_at": 1},
                    "seven_day": {"used_percent": 37, "remaining_percent": 63, "resets_at": 2},
                },
            },
        )
        rec = quota.read_external("claude")
        self.assertEqual({w["name"] for w in rec["windows"]}, {"five_hour", "seven_day"})
        # Route on the WORST window: a weekly pool at 3% is not rescued by a
        # five-hour window that just reset.
        self.assertEqual(rec["min_remaining_percent"], 63)
        self.assertFalse(rec["stale"])

    def test_list_shaped_windows_are_normalized(self):
        self._write(
            "codex",
            {
                "provider": "codex",
                "captured_at": time.time(),
                "windows": [
                    {"limit_id": "codex", "remaining_percent": 31, "resets_at": 1},
                    {"limit_id": "codex_other", "remaining_percent": 100, "resets_at": 2},
                ],
            },
        )
        rec = quota.read_external("codex")
        self.assertEqual(rec["min_remaining_percent"], 31)
        self.assertIn("codex", [w["name"] for w in rec["windows"]])

    def test_an_old_reading_is_reported_stale_not_silently_trusted(self):
        """A stale reading is worse than none: it invites a confident wrong call."""
        self._write(
            "codex",
            {"captured_at": time.time() - (quota.STALE_AFTER_S * 3), "windows": []},
        )
        self.assertTrue(quota.read_external("codex")["stale"])

    def test_a_reading_with_no_timestamp_is_stale(self):
        self._write("codex", {"windows": []})
        rec = quota.read_external("codex")
        self.assertTrue(rec["stale"])
        self.assertIsNone(rec["age_s"])

    def test_missing_or_corrupt_files_are_absent_not_fatal(self):
        self.assertIsNone(quota.read_external("nope"))
        (self.tmp / "torn.json").write_text("{not json")
        self.assertIsNone(quota.read_external("torn"))


class Budget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-budget-"))
        self.state = str(self.tmp / "state")
        os.makedirs(self.state)
        self.budget = self.tmp / "budget.json"
        os.environ["SWITCHGEAR_BUDGET_FILE"] = str(self.budget)

    def tearDown(self):
        os.environ.pop("SWITCHGEAR_BUDGET_FILE", None)

    def test_no_budget_file_means_unlimited_and_never_refuses(self):
        quota.assert_within_budget(self.state)  # must not raise

    def test_spend_accumulates_and_the_ceiling_refuses(self):
        self.budget.write_text(json.dumps({"daily_usd": 0.10}))
        quota.record_spend(self.state, "j1", "m", 0.04)
        quota.assert_within_budget(self.state)  # 0.04 < 0.10, still fine
        quota.record_spend(self.state, "j2", "m", 0.07)
        with self.assertRaises(Refuse) as cm:
            quota.assert_within_budget(self.state)
        msg = str(cm.exception)
        # The refusal has to say how to proceed, or it is just a wall.
        self.assertIn("daily budget exhausted", msg)
        self.assertIn(str(self.budget), msg)

    def test_yesterdays_spend_does_not_count_against_today(self):
        self.budget.write_text(json.dumps({"daily_usd": 0.10}))
        with open(quota.ledger_path(self.state), "a") as fh:
            fh.write(json.dumps({"ts": time.time() - 200000, "costUSD": 99.0}) + "\n")
        quota.assert_within_budget(self.state)

    def test_a_torn_ledger_line_does_not_refuse_every_job(self):
        """The ledger is appended by every job; a half-written final line must
        degrade the accounting, not brick the rail."""
        self.budget.write_text(json.dumps({"daily_usd": 1.0}))
        quota.record_spend(self.state, "j1", "m", 0.01)
        with open(quota.ledger_path(self.state), "a") as fh:
            fh.write('{"ts": 123, "costU')
        self.assertAlmostEqual(quota.spent_since(self.state, 0), 0.01)
        quota.assert_within_budget(self.state)

    def test_recording_spend_never_raises_into_the_job_path(self):
        """A job that already ran already cost money. Losing the accounting is
        bad; losing the work as well is worse."""
        quota.record_spend("/nonexistent/path/that/cannot/exist", "j", "m", 1.0)

    def test_call_ceiling_is_operator_owned_and_off_by_default(self):
        self.assertIsNone(quota.max_provider_calls())
        self.budget.write_text(json.dumps({"max_provider_calls_per_job": 6}))
        self.assertEqual(quota.max_provider_calls(), 6)


class CallCeiling(unittest.TestCase):
    def test_broker_denies_past_the_ceiling(self):
        """Earned by a reviewer that looped 35 times looking for a diff it could
        not reach. The job timeout was the only bound, and every call was billed.

        The ceiling counts ATTEMPTS, not forwards. `forwarded` means "a model
        answered" -- live.sh's retry rule depends on that meaning -- so a runaway
        loop whose calls all fail upstream never increments it, and a ceiling
        counting forwards would never fire on the case that most needs bounding.
        This test uses a dead upstream precisely so nothing is ever forwarded:
        the first two attempts fail at the upstream (502) and the rest are
        refused (429) without a request leaving the controller.
        """
        import urllib.error
        import urllib.request

        from switchgear.broker import CredentialBroker

        with CredentialBroker(
            "SECRET", upstream="http://127.0.0.1:9/v1",
            allowed_models={"m/x"}, max_calls=2,
        ) as bk:
            codes = []
            for _ in range(4):
                req = urllib.request.Request(
                    bk.base_url + "/v1/chat/completions",
                    data=json.dumps({"model": "m/x"}).encode(),
                    headers={"content-type": "application/json"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=5)
                    codes.append(200)
                except urllib.error.HTTPError as exc:
                    codes.append(exc.code)
                except Exception:
                    codes.append(0)
            self.assertEqual(codes, [502, 502, 429, 429])
            self.assertEqual(bk.forwarded, 0, "nothing reached a model")
            self.assertEqual(bk.attempts, 2, "attempts stopped at the ceiling")
            self.assertTrue(any("ceiling" in d for d in bk.denials))

    def test_no_ceiling_configured_means_no_limit(self):
        from switchgear.broker import CredentialBroker

        with CredentialBroker("S", upstream="http://127.0.0.1:9/v1") as bk:
            self.assertIsNone(bk.max_calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
