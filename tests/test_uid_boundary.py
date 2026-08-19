#!/usr/bin/env python3
"""The second containment layer: the worker is not you.

Until this existed, isolation was mount VISIBILITY only — the worker ran as the
invoking user, so anything that became reachable was also writable. One layer.

The naive version of this is worse than none, and these tests exist mostly to
pin that distinction: `bwrap --unshare-user --uid N` produces a namespace whose
map has one entry, so the invoking uid maps to the sandbox's own uid, your files
appear to be owned by the worker, and they stay writable. It reports a different
number in `id -u` and confines nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "python" / "switchgear" / "__main__.py"
MOCK = ROOT / "tests" / "helpers" / "mock_provider.py"
PYTHON = "/usr/bin/python3"
sys.path.insert(0, str(ROOT / "python"))

from switchgear import userns  # noqa: E402


def available():
    return userns.capability().get("available")


class Capability(unittest.TestCase):
    def test_it_reports_why_it_is_unavailable(self):
        """A silent 'unavailable' is indistinguishable from 'not attempted', and
        a boundary you cannot verify is not one."""
        os.environ["SWITCHGEAR_NO_UID_BOUNDARY"] = "1"
        try:
            cap = userns.capability()
        finally:
            os.environ.pop("SWITCHGEAR_NO_UID_BOUNDARY", None)
        self.assertFalse(cap["available"])
        self.assertIn("SWITCHGEAR_NO_UID_BOUNDARY", cap["reason"])

    def test_the_payload_id_is_never_the_mapped_root(self):
        """inside-0 maps to the invoking user — it is exactly the identity this
        exists to escape, so the payload must not run there."""
        self.assertNotEqual(userns.PAYLOAD_UID, 0)
        self.assertIn("--reuid", userns.payload_prefix())
        self.assertIn(str(userns.PAYLOAD_UID), userns.payload_prefix())


@unittest.skipUnless(available(), "no subuid range / uidmap tools on this machine")
class RealBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-uidb-"))
        self.state = self.tmp / "state"
        out = subprocess.run(["bash", str(ROOT / "tests" / "helpers" / "make-synthetic-repo"),
                              str(self.tmp / "syn")], capture_output=True, text=True)
        self.vals = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
        self.profile = self.tmp / "p.json"
        self.profile.write_text((ROOT / "project-profiles" / "example.json").read_text())
        subprocess.run([PYTHON, str(MAIN), "--state", str(self.state),
                        "state", "provision", str(self.state)], capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scout(self, behaviour):
        p = subprocess.run(
            [PYTHON, str(MAIN), "--profile", str(self.profile), "--state", str(self.state),
             "--provider", str(MOCK), "--json", "scout", self.vals["PRIMARY"], "x"],
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ, SWITCHGEAR_MOCK_BEHAVIOR=behaviour))
        return p, (json.loads(p.stdout) if p.stdout.strip().startswith("{") else {})

    def test_a_readonly_worker_does_not_run_as_the_invoking_user(self):
        p, rec = self._scout("whoami")
        self.assertEqual(p.returncode, 0, p.stderr)
        events = Path(rec["artifacts"]["events"]).read_text()
        self.assertIn(f"uid={userns.PAYLOAD_UID}", events,
                      f"worker did not drop to the payload id: {events[:200]}")
        self.assertNotIn(f"uid={os.getuid()}", events,
                         "the worker ran as the invoking user")

    def test_the_worker_still_reaches_the_directories_made_for_it(self):
        """The boundary is useless if it also locks the worker out of its own
        HOME. Measured: it did, and the provider silently fell back to defaults
        because it could not read its own config."""
        p, rec = self._scout("whoami")
        self.assertEqual(p.returncode, 0, p.stderr)
        # Reaching the behaviour file at all is the proof: it lives in the
        # synthetic HOME, which the controller creates 0700 as itself.
        self.assertIn("uid=", Path(rec["artifacts"]["events"]).read_text())

    def test_bounded_write_does_not_claim_the_boundary(self):
        """Excluded on purpose: files written by a subuid are owned by it, and
        the controller cannot hand them back. Claiming it for that lane would
        trade a real property for a broken one."""
        import inspect

        from switchgear import job as jobmod

        src = inspect.getsource(jobmod)
        marker = src[src.index("uid_boundary = None"):]
        self.assertIn('if mode == "readonly"', marker[:400],
                      "the boundary must be gated on readonly")

    def test_a_failed_map_kills_the_job_rather_than_running_unconfined(self):
        """If the map cannot be written, bwrap would run the payload as the
        invoking user while the caller believed it was confined."""
        import inspect

        from switchgear import process as procmod

        src = inspect.getsource(procmod.run_sandboxed)
        self.assertIn("Never unblock a namespace whose map failed", src)
        self.assertIn("SIGKILL", src)


class RealizedFacts(unittest.TestCase):
    """`security` must report what the job GOT, not what it asked for.

    The two cases that matter are identical in the policy and in the request: a
    readonly job on a machine that can establish the boundary, and the same job
    on a machine that cannot. If the record cannot tell them apart it is
    describing intent, and a caller that tests it learns nothing.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-facts-"))
        self.state = self.tmp / "state"
        out = subprocess.run(["bash", str(ROOT / "tests" / "helpers" / "make-synthetic-repo"),
                              str(self.tmp / "syn")], capture_output=True, text=True)
        self.vals = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
        self.profile = self.tmp / "p.json"
        self.profile.write_text((ROOT / "project-profiles" / "example.json").read_text())
        subprocess.run([PYTHON, str(MAIN), "--state", str(self.state),
                        "state", "provision", str(self.state)], capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scout(self, **env):
        p = subprocess.run(
            [PYTHON, str(MAIN), "--profile", str(self.profile), "--state", str(self.state),
             "--provider", str(MOCK), "--json", "scout", self.vals["PRIMARY"], "x"],
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ, SWITCHGEAR_MOCK_BEHAVIOR="ok", **env))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["security"]

    @unittest.skipUnless(available(), "this machine cannot establish a uid boundary")
    def test_it_reports_the_boundary_when_the_machine_has_one(self):
        sec = self._scout()
        self.assertEqual(sec["identity"]["uid_boundary"], "subuid")
        self.assertEqual(sec["identity"]["payload_uid"], userns.PAYLOAD_UID)

    def test_it_reports_same_user_when_the_boundary_is_unavailable(self):
        """The half that proves the field is not a restatement of the request.
        Same profile, same role, same mode -- only the machine's capability
        differs, and the record must say so."""
        sec = self._scout(SWITCHGEAR_NO_UID_BOUNDARY="1")
        self.assertEqual(sec["identity"]["uid_boundary"], "same-user")
        self.assertIsNone(sec["identity"]["payload_uid"])

    def test_namespace_facts_come_from_the_argv_that_was_built(self):
        """Read back off the constructed sandbox argv. A field populated from the
        policy would report the same value whether or not the flag was passed."""
        sec = self._scout()
        self.assertEqual(sec["containment"]["backend"], "bwrap")
        for ns in ("mount_namespace", "pid_namespace", "ipc_namespace", "uts_namespace"):
            self.assertTrue(sec["containment"][ns], ns)
        # The hermetic mock has no broker, so there is no network namespace to
        # claim -- and the record must not claim one. This is the case that would
        # silently read `true` from an intent-derived field.
        self.assertFalse(sec["containment"]["network_namespace"])
        self.assertFalse(sec["network"]["broker_only"])
        self.assertEqual(sec["credential"]["posture"], "none")
        self.assertFalse(sec["credential"]["enters_worker"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
