#!/usr/bin/env python3
"""Provider health and cost rollup: both aggregate records that already exist.

The load-bearing property for health is that it REPORTS and never gates. Part of
this tool's audience is an AI agent driving it, and an agent testing a fix for a
failing model must not be refused from testing its own fix.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "python" / "ai_ops" / "__main__.py"
PYTHON = "/usr/bin/python3"
sys.path.insert(0, str(ROOT / "python"))

from ai_ops import health, quota  # noqa: E402


def run_cli(args, timeout=60):
    return subprocess.run([PYTHON, str(MAIN), *args], capture_output=True,
                          text=True, env=os.environ.copy(), timeout=timeout)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-health-"))
        self.state = self.tmp / "state"
        p = run_cli(["--state", str(self.state), "state", "provision", str(self.state)])
        self.assertEqual(p.returncode, 0, p.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _job(self, suffix, status, model, calls=None):
        job_id = f"00000000-0000-4000-8000-{suffix:012x}"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time() - 100))
        rec = {"status": status, "role": "scout", "mode": "readonly",
               "model": {"id": model, "provider": model.split("/")[0]}}
        if calls:
            rec["provider_calls"] = calls
        (jd / "result.json").write_text(json.dumps(rec))
        return job_id


class Health(Base):
    def test_a_failing_model_is_reported(self):
        for i in range(4):
            self._job(i, "provider_error", "pool/dead-model")
        seen = health.observe(str(self.state))
        row = next(m for m in seen["models"] if m["model"] == "pool/dead-model")
        self.assertEqual(row["failed"], 4)
        self.assertEqual(row["failure_ratio"], 1.0)
        self.assertTrue(row["unhealthy"])

    def test_a_healthy_model_is_not_flagged(self):
        for i in range(4):
            self._job(i, "ok", "pool/good-model")
        row = next(m for m in health.observe(str(self.state))["models"]
                   if m["model"] == "pool/good-model")
        self.assertFalse(row["unhealthy"])

    def test_too_few_observations_is_not_a_signal(self):
        """Two failures out of two is not evidence a model is dead."""
        for i in range(2):
            self._job(i, "provider_error", "pool/new-model")
        row = next(m for m in health.observe(str(self.state))["models"]
                   if m["model"] == "pool/new-model")
        self.assertEqual(row["failure_ratio"], 1.0)
        self.assertFalse(row["unhealthy"], "flagged on too little evidence")

    def test_running_and_unknown_jobs_have_no_outcome(self):
        """Counting a job with no established outcome as a failure would
        manufacture bad news out of missing information."""
        job_id = "00000000-0000-4000-8000-0000000000ff"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time()))
        # no result.json, no runner.json -> `unknown`
        seen = health.observe(str(self.state))
        self.assertEqual(seen["models"], [])

    def test_denied_and_transport_are_kept_apart(self):
        """A policy denial and an upstream blip need different actions;
        blending them would hide both."""
        self._job(1, "ok", "pool/m", calls={"denied": 3, "transport": 0, "forwarded": 1})
        self._job(2, "ok", "pool/m", calls={"denied": 0, "transport": 5, "forwarded": 1})
        row = next(m for m in health.observe(str(self.state))["models"]
                   if m["model"] == "pool/m")
        self.assertEqual(row["denied"], 3)
        self.assertEqual(row["transport"], 5)

    def test_health_never_refuses_a_job(self):
        """The decisive property. An unhealthy model must still be runnable —
        an agent testing a fix for it would otherwise be locked out."""
        import inspect

        from ai_ops import job as jobmod

        src = inspect.getsource(jobmod)
        self.assertNotIn("health", src,
                         "run_job must not consult health; it reports, never gates")

    def test_warnings_are_empty_when_all_is_well(self):
        for i in range(4):
            self._job(i, "ok", "pool/fine")
        self.assertEqual(health.warnings(str(self.state)), [])

    def test_doctor_reports_health_as_warn_never_fail(self):
        for i in range(5):
            self._job(i, "provider_error", "pool/dead")
        p = run_cli(["--state", str(self.state), "--json", "doctor"])
        out = json.loads(p.stdout)
        entries = [c for c in out["checks"] if c["name"].startswith("health.")]
        self.assertTrue(entries)
        for c in entries:
            self.assertNotEqual(c["status"], "fail",
                                "an unhealthy model must never fail the doctor")


class Rollup(Base):
    def _spend(self, model, cost, ts=None):
        quota.record_spend(str(self.state), "j", model, cost)
        if ts is not None:  # rewrite the timestamp for day bucketing
            path = quota.ledger_path(str(self.state))
            lines = Path(path).read_text().splitlines()
            rec = json.loads(lines[-1]); rec["ts"] = ts
            lines[-1] = json.dumps(rec)
            Path(path).write_text("\n".join(lines) + "\n")

    def test_aggregates_by_provider_model_and_day(self):
        self._spend("claude/haiku", 0.10)
        self._spend("claude/sonnet", 0.20)
        self._spend("grok/grok-4.5", 0.05)
        roll = quota.rollup(str(self.state))
        self.assertAlmostEqual(roll["total_usd"], 0.35, places=6)
        self.assertEqual(roll["jobs"], 3)
        top = roll["by_provider"][0]
        self.assertEqual(top["name"], "claude")
        self.assertAlmostEqual(top["cost_usd"], 0.30, places=6)
        self.assertEqual(len(roll["by_model"]), 3)
        self.assertEqual(len(roll["by_day"]), 1)

    def test_zero_cost_is_labelled_unmetered_not_free(self):
        """Measured: Codex on a subscription reports no per-step cost. Reporting
        that as $0 would invite routing everything there believing it is free."""
        self._spend("codex/gpt-x", 0.0)
        self._spend("claude/haiku", 0.10)
        roll = quota.rollup(str(self.state))
        self.assertIn("codex", roll["unmetered_providers"])
        codex = next(r for r in roll["by_provider"] if r["name"] == "codex")
        self.assertFalse(codex["metered"])
        claude = next(r for r in roll["by_provider"] if r["name"] == "claude")
        self.assertTrue(claude["metered"])

    def test_a_torn_final_line_does_not_break_the_rollup(self):
        """The ledger is appended to while jobs run, so the last line is
        routinely half written."""
        self._spend("claude/haiku", 0.10)
        with open(quota.ledger_path(str(self.state)), "a") as fh:
            fh.write('{"ts":123,"model":"x/y","cost')
        roll = quota.rollup(str(self.state))
        self.assertAlmostEqual(roll["total_usd"], 0.10, places=6)

    def test_the_daily_budget_reading_is_unchanged_by_the_rollup(self):
        """assert_within_budget reads spent_since and nothing else; the rollup
        must be a pure read alongside it."""
        self._spend("claude/haiku", 0.10)
        before = quota.spent_since(str(self.state), quota.day_start())
        quota.rollup(str(self.state))
        self.assertEqual(quota.spent_since(str(self.state), quota.day_start()), before)

    def test_empty_ledger_is_zero_not_an_error(self):
        roll = quota.rollup(str(self.state))
        self.assertEqual(roll["total_usd"], 0.0)
        self.assertEqual(roll["by_provider"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
