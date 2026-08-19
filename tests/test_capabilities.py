#!/usr/bin/env python3
"""`capabilities` must be DERIVED, not written down twice.

A hand-maintained capability document is stale the day after it is written — not
hypothetically, the model registry had already drifted to 18 hand-listed ids
where the provider served 26. These tests are what keep this one honest: each
asserts the reported surface equals the real one, so drift fails CI instead of
producing a confident, incomplete answer to an agent that cannot check.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "python" / "switchgear" / "__main__.py"
PYTHON = "/usr/bin/python3"
sys.path.insert(0, str(ROOT / "python"))

from switchgear import capabilities as capmod  # noqa: E402
from switchgear.cli import build_parser  # noqa: E402


def run_cli(args, timeout=60):
    return subprocess.run([PYTHON, str(MAIN), *args], capture_output=True,
                          text=True, env=os.environ.copy(), timeout=timeout)


class Honesty(unittest.TestCase):
    def setUp(self):
        self.out = capmod.describe(build_parser(), None, None)

    def test_the_provider_set_equals_the_adapter_registry(self):
        """The check that would have caught the model registry drifting. Adding
        a fifth provider without wiring it in must fail here."""
        from switchgear.adapters import _ADAPTERS
        from switchgear.compat import PINNED_PROVIDERS

        reported = {p["provider"] for p in self.out["providers"]}
        self.assertEqual(reported, set(_ADAPTERS) | set(PINNED_PROVIDERS))
        for p in self.out["providers"]:
            if p["provider"] in _ADAPTERS:
                self.assertTrue(p["has_adapter"])

    def test_the_command_set_equals_the_parser(self):
        parser = build_parser()
        real = set()
        import argparse as ap

        for action in parser._actions:
            if isinstance(action, ap._SubParsersAction):
                real |= set(action.choices)
        self.assertEqual({c["command"] for c in self.out["commands"]}, real)

    def test_every_command_documents_itself(self):
        """A self-describing surface that says nothing is worse than none: an
        agent would take the empty string as the answer."""
        undocumented = [c["command"] for c in self.out["commands"] if not c["help"]]
        self.assertEqual(undocumented, [], f"commands with no help: {undocumented}")

    def test_effort_matches_each_adapter(self):
        from switchgear.adapters import _ADAPTERS

        for p in self.out["providers"]:
            adapter = _ADAPTERS.get(p["provider"])
            if adapter:
                self.assertEqual(p["effort"]["status"],
                                 adapter.effort_support()["status"], p["provider"])

    def test_resume_matches_the_measured_session_stores(self):
        from switchgear.adapters import _ADAPTERS

        for p in self.out["providers"]:
            adapter = _ADAPTERS.get(p["provider"])
            if adapter:
                self.assertEqual(p["can_resume"], bool(adapter.session_store_paths()))

    def test_the_exit_table_matches_the_one_the_code_uses(self):
        """Two copies of an exit-code table is how they drift; this asserts the
        published contract against jobstate.exit_code_for itself."""
        from switchgear.jobstate import exit_code_for

        self.assertEqual(exit_code_for("dirty"), 2)
        self.assertEqual(exit_code_for("timeout"), 124)
        self.assertEqual(exit_code_for("ok"), 0)
        self.assertEqual(exit_code_for("awaiting_review"), 0)
        self.assertEqual(exit_code_for("provider_error"), 1)
        published = set(self.out["refusals"]["exit_codes"])
        self.assertEqual(published, {"0", "1", "2", "124"})

    def test_the_refusal_prefix_is_the_one_actually_printed(self):
        """A caller parses stderr on this string; if it drifts, they break."""
        p = run_cli(["--state", "/nonexistent-abs-path", "jobs"])
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(
            p.stderr.startswith(self.out["refusals"]["stderr_prefix"]),
            f"published prefix does not match reality: {p.stderr[:80]!r}")

    def test_flags_come_from_the_parser(self):
        gc = next(c for c in self.out["commands"] if c["command"] == "gc")
        names = {f["name"] for f in gc["flags"]}
        self.assertIn("--older-than", names)
        self.assertIn("--yes", names)
        for f in gc["flags"]:
            self.assertTrue(f["help"], f"flag {f['name']} has no help")


class Usable(unittest.TestCase):
    def test_it_answers_without_a_profile_or_state_root(self):
        """A caller asking what the tool can do needs an answer most when the
        configuration is broken."""
        p = run_cli(["--profile", "/nonexistent.json", "--json", "capabilities"])
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertTrue(out["commands"])
        self.assertTrue(out["providers"])

    def test_json_and_text_agree_on_the_command_set(self):
        pj = run_cli(["--json", "capabilities"])
        pt = run_cli(["capabilities"])
        cmds = {c["command"] for c in json.loads(pj.stdout)["commands"]}
        for name in cmds:
            self.assertIn(name, pt.stdout)

    def test_it_reports_containment_honestly(self):
        out = capmod.describe(build_parser(), None, None)
        self.assertEqual(out["containment"]["platform"], "linux-only")
        self.assertIn("NOT a uid boundary", out["containment"]["note"])
        # A platform limit must come with what to do about it, like every other
        # bad news this tool reports.
        self.assertIn("VM", out["containment"]["elsewhere"])

    def test_the_non_linux_remedy_does_not_send_people_after_apt(self):
        """Telling a macOS user to `apt install bubblewrap` is worse than saying
        nothing: it sends them after a package that does not exist for their OS
        and hides that this is a platform limit, not a missing dependency."""
        import platform as plat

        from switchgear import doctor, sandbox

        real_system, real_bwrap = plat.system, sandbox.TRUSTED_BWRAP
        plat.system = lambda: "Darwin"
        sandbox.TRUSTED_BWRAP = "/nonexistent/bwrap"
        try:
            check = doctor.check_sandbox()[0]
        finally:
            plat.system, sandbox.TRUSTED_BWRAP = real_system, real_bwrap
        self.assertEqual(check["status"], "fail")
        self.assertNotIn("apt install", check["remedy"])
        self.assertIn("Linux-only", check["remedy"])
        self.assertIn("PORTABILITY", check["remedy"])


class RemediesAreRunnable(unittest.TestCase):
    """A remedy that names a command which does not exist is worse than none.

    Found the hard way: the refusal for an unverified provider build said to run
    `switchgear providers verify --provider claude`, and the parser only accepted
    `--verify`. The remedy errored. Every one of these strings is written by
    someone confident it works, which is exactly why it needs a machine check.
    """

    def _invocations(self, text):
        """Every `switchgear ...` command mentioned in a string."""
        import re

        out = []
        for m in re.finditer(r"switchgear\s+([^`'\"\n.;]+)", text or ""):
            argv = m.group(1).split()
            # Trim trailing prose the regex swept up.
            while argv and argv[-1] in {"—", "-", "and", "or", "then", "to", "it"}:
                argv.pop()
            if argv:
                out.append(argv)
        return out

    def _parses(self, argv):
        import argparse
        import contextlib
        import io

        parser = build_parser()
        placeholders = {"<root>", "<job>", "<abs", "N", "<duration>", "<provider>",
                        "<path>", "<state>", "<s>", "<subject>", "<review>"}
        cleaned = [a for a in argv if not (a.startswith("<") or a in placeholders)]
        if not cleaned:
            return True
        with contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                parser.parse_known_args(cleaned)
            except SystemExit:
                return False
            except Exception:
                return False
        return True

    def test_every_doctor_remedy_names_a_real_command(self):
        from switchgear import doctor

        bad = []
        for check in doctor.run_all(None)["checks"]:
            for argv in self._invocations(check.get("remedy", "")):
                if not self._parses(argv):
                    bad.append((check["name"], " ".join(argv)))
        self.assertEqual(bad, [], f"remedies naming commands that do not parse: {bad}")

    def test_the_specific_form_that_was_broken(self):
        """`providers verify` is the phrasing this codebase reached for in five
        independent places; the parser now accepts it as well as --verify."""
        self.assertTrue(self._parses(["providers", "verify", "--provider", "claude"]))
        self.assertTrue(self._parses(["providers", "--provider", "claude", "--verify"]))

    def test_the_check_can_actually_fail(self):
        """Guard against the guard passing vacuously."""
        self.assertFalse(self._parses(["providers", "definitely-not-an-action"]))
        self.assertFalse(self._parses(["not-a-command"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
