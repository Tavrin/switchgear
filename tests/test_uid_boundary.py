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
MAIN = ROOT / "python" / "ai_ops" / "__main__.py"
MOCK = ROOT / "tests" / "helpers" / "mock_provider.py"
PYTHON = "/usr/bin/python3"
sys.path.insert(0, str(ROOT / "python"))

from ai_ops import userns  # noqa: E402


def available():
    return userns.capability().get("available")


class Capability(unittest.TestCase):
    def test_it_reports_why_it_is_unavailable(self):
        """A silent 'unavailable' is indistinguishable from 'not attempted', and
        a boundary you cannot verify is not one."""
        os.environ["AI_OPS_NO_UID_BOUNDARY"] = "1"
        try:
            cap = userns.capability()
        finally:
            os.environ.pop("AI_OPS_NO_UID_BOUNDARY", None)
        self.assertFalse(cap["available"])
        self.assertIn("AI_OPS_NO_UID_BOUNDARY", cap["reason"])

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
            env=dict(os.environ, AI_OPS_MOCK_BEHAVIOR=behaviour))
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

        from ai_ops import job as jobmod

        src = inspect.getsource(jobmod)
        marker = src[src.index("uid_boundary = None"):]
        self.assertIn('if mode == "readonly"', marker[:400],
                      "the boundary must be gated on readonly")

    def test_a_failed_map_kills_the_job_rather_than_running_unconfined(self):
        """If the map cannot be written, bwrap would run the payload as the
        invoking user while the caller believed it was confined."""
        import inspect

        from ai_ops import process as procmod

        src = inspect.getsource(procmod.run_sandboxed)
        self.assertIn("Never unblock a namespace whose map failed", src)
        self.assertIn("SIGKILL", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
