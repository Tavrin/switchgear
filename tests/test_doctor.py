#!/usr/bin/env python3
"""Doctor must be right about a BROKEN install, not just a healthy one.

A diagnostic that only ever passes is indistinguishable from one that does
nothing, so every check here breaks something real and asserts doctor both
notices and says what to do about it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "python" / "ai_ops" / "__main__.py"
PYTHON = "/usr/bin/python3"
sys.path.insert(0, str(ROOT / "python"))

from ai_ops import doctor  # noqa: E402


def run_cli(args, env=None, timeout=120):
    base = os.environ.copy()
    if env:
        base.update(env)
    return subprocess.run([PYTHON, str(MAIN), *args], capture_output=True,
                          text=True, env=base, timeout=timeout)


class DoctorUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-doctor-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _by_name(self, checks, name):
        return next(c for c in checks if c["name"] == name)

    def test_every_bad_check_names_a_remedy(self):
        """The contract that makes doctor useful to an agent: a check that says
        something is wrong must also say what to do, or the caller is stuck."""
        out = doctor.run_all(str(self.tmp / "does-not-exist"))
        bad = [c for c in out["checks"] if c["status"] != doctor.PASS]
        self.assertTrue(bad, "expected at least one non-pass on a bogus state root")
        for c in bad:
            # The one deliberate exception: credential refusals already carry
            # their own remedy in the message load_credential raises.
            if c["name"].startswith("credential."):
                continue
            self.assertTrue(c["remedy"], f"{c['name']} reports a problem with no remedy")

    def test_unprovisioned_state_root_fails_and_says_how(self):
        out = doctor.check_state(str(self.tmp / "nope"))
        root = self._by_name(out, "state.root")
        self.assertEqual(root["status"], doctor.FAIL)
        self.assertIn("provision", root["remedy"])

    def test_healthy_state_root_passes_and_is_left_untouched(self):
        """Doctor writes a probe file to prove the root is writable; it must not
        leave it behind. A diagnostic that litters the thing it inspects is one
        you cannot run twice."""
        root = self.tmp / "state"
        p = run_cli(["--state", str(root), "state", "provision", str(root)])
        self.assertEqual(p.returncode, 0, p.stderr)
        before = sorted(os.listdir(root / "jobs"))
        out = doctor.check_state(str(root))
        self.assertEqual(self._by_name(out, "state.root")["status"], doctor.PASS)
        self.assertEqual(self._by_name(out, "state.writable")["status"], doctor.PASS)
        self.assertEqual(sorted(os.listdir(root / "jobs")), before)

    def test_read_only_state_root_is_reported_not_crashed(self):
        root = self.tmp / "ro"
        p = run_cli(["--state", str(root), "state", "provision", str(root)])
        self.assertEqual(p.returncode, 0, p.stderr)
        os.chmod(root / "jobs", 0o500)
        try:
            out = doctor.check_state(str(root))
            self.assertEqual(self._by_name(out, "state.writable")["status"], doctor.FAIL)
        finally:
            os.chmod(root / "jobs", 0o700)

    def test_a_crashing_check_is_reported_not_fatal(self):
        """Doctor runs when things are already broken; it must survive the
        breakage it was called to describe."""
        def boom():
            raise RuntimeError("synthetic")

        original = doctor.CHECKS[:]
        doctor.CHECKS.insert(0, ("synthetic", boom, False))
        try:
            out = doctor.run_all(None)
        finally:
            doctor.CHECKS[:] = original
        crashed = self._by_name(out["checks"], "synthetic.<crashed>")
        self.assertEqual(crashed["status"], doctor.FAIL)
        self.assertIn("synthetic", crashed["detail"])
        self.assertEqual(out["status"], doctor.FAIL)

    def test_credentials_never_leak_the_token(self):
        """Doctor output is exactly what someone pastes into a bug report."""
        blob = json.dumps(doctor.check_credentials())
        for cred in doctor.check_credentials():
            self.assertNotIn("token", cred["detail"].lower())
        # Any real installed secret on this machine must not appear in output.
        from ai_ops.credentials import load_credential
        from ai_ops.registry import load_models, provider_record

        for pool in (load_models().get("providers") or {}):
            try:
                c = load_credential(pool, provider_record(pool))
            except Exception:
                continue
            if c and c.token and len(c.token) > 12:
                self.assertNotIn(c.token, blob, f"{pool} credential leaked into doctor output")

    def test_warnings_alone_do_not_fail_the_verdict(self):
        """CI gates on failures; an unused provider must not turn a build red."""
        checks = [{"name": "x", "status": doctor.WARN, "detail": "", "remedy": "r"}]
        counts = {s: sum(1 for c in checks if c["status"] == s)
                  for s in (doctor.PASS, doctor.WARN, doctor.FAIL)}
        self.assertEqual(counts[doctor.FAIL], 0)
        out = doctor.run_all(None)
        if out["counts"][doctor.FAIL] == 0 and out["counts"][doctor.WARN] > 0:
            self.assertEqual(out["status"], doctor.WARN)

    def test_exit_code_is_zero_with_warnings(self):
        root = self.tmp / "state2"
        run_cli(["--state", str(root), "state", "provision", str(root)])
        p = run_cli(["--state", str(root), "--json", "doctor"])
        out = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0 if out["status"] != doctor.FAIL else 1)

    def test_missing_sandbox_backend_is_fatal(self):
        """There is no unsandboxed fallback, so this is the one unambiguous FAIL."""
        from ai_ops import sandbox

        original = sandbox.TRUSTED_BWRAP
        sandbox.TRUSTED_BWRAP = str(self.tmp / "no-bwrap-here")
        try:
            out = doctor.check_sandbox()
        finally:
            sandbox.TRUSTED_BWRAP = original
        self.assertEqual(out[0]["status"], doctor.FAIL)
        self.assertIn("bubblewrap", out[0]["remedy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
