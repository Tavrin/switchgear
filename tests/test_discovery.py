#!/usr/bin/env python3
"""Provider installs are discovered or operator-declared, never hardcoded.

compat.py used to hold six absolute paths under one home directory, which meant
the package worked on exactly one machine. That is fatal for distribution and for
CI, and invisible while you only ever run it in one place.

The security property that must survive the change: an unrecognised real provider
binary is treated as a committed mock, which is fail-closed for credentials but
runs a real agent without the live gate. So recognition has to cover every
installed build, not just the newest.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-disc-"))
        self._env = {k: os.environ.get(k) for k in ("HOME", "SWITCHGEAR_PROVIDERS_FILE")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reload(self, home=None, providers_file=None):
        import importlib

        os.environ["HOME"] = str(home or self.tmp / "home")
        os.environ["SWITCHGEAR_PROVIDERS_FILE"] = str(
            providers_file or self.tmp / "no-such-providers.json")
        from switchgear import compat

        return importlib.reload(compat)

    def _fake_binary(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho fake\n")
        path.chmod(0o755)
        return path

    def test_a_machine_with_nothing_installed_reports_no_paths(self):
        """Not an error and not an omission: doctor and capabilities must be able
        to say 'not installed' rather than silently not mentioning a provider."""
        compat = self._reload()
        self.assertTrue(compat.PINNED_PROVIDERS, "providers vanished entirely")
        for name, rec in compat.PINNED_PROVIDERS.items():
            self.assertIsNone(rec["path"], name)
            self.assertEqual(rec["paths"], [], name)
            self.assertIsNotNone(rec["version"], f"{name} lost its version pin")

    def test_a_discovered_install_is_found(self):
        home = self.tmp / "home"
        self._fake_binary(home / ".opencode" / "bin" / "opencode")
        compat = self._reload(home=home)
        self.assertEqual(compat.PINNED_PROVIDERS["opencode"]["path"],
                         str(home / ".opencode" / "bin" / "opencode"))

    def test_every_installed_build_is_recognised_not_just_the_newest(self):
        """A self-updating CLI keeps several versions installed. Recognising only
        one would classify the others as committed mocks and run a real agent
        without the live gate."""
        home = self.tmp / "home"
        versions = home / ".local" / "share" / "claude" / "versions"
        old = self._fake_binary(versions / "2.1.100")
        new = self._fake_binary(versions / "2.1.200")
        os.utime(old, (time.time() - 10_000, time.time() - 10_000))
        compat = self._reload(home=home)

        rec = compat.PINNED_PROVIDERS["claude"]
        self.assertEqual(rec["path"], str(new), "primary should be the newest build")
        self.assertIn(str(old), rec["paths"])
        for path in (old, new):
            match = compat.pinned_for_path(str(path))
            self.assertIsNotNone(match, f"{path} not recognised as a live provider")
            self.assertEqual(match[0], "claude")

    def test_a_symlinked_launcher_resolves_to_the_same_identity(self):
        home = self.tmp / "home"
        real = self._fake_binary(home / ".grok" / "downloads" / "grok-linux-x86_64")
        link_dir = home / ".grok" / "bin"
        link_dir.mkdir(parents=True, exist_ok=True)
        link = link_dir / "grok"
        os.symlink(real, link)
        compat = self._reload(home=home)
        self.assertEqual(compat.pinned_for_path(str(link))[0], "grok")

    def test_a_non_executable_file_is_not_a_provider(self):
        home = self.tmp / "home"
        p = home / ".opencode" / "bin" / "opencode"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not executable")
        p.chmod(0o644)
        compat = self._reload(home=home)
        self.assertIsNone(compat.PINNED_PROVIDERS["opencode"]["path"])

    def test_an_unrelated_binary_is_never_recognised(self):
        """The whole live/mock decision rests on this."""
        home = self.tmp / "home"
        self._fake_binary(home / ".opencode" / "bin" / "opencode")
        compat = self._reload(home=home)
        self.assertIsNone(compat.pinned_for_path("/bin/ls"))
        self.assertIsNone(compat.pinned_for_path(str(self.tmp / "nothing")))


class OperatorOverride(Discovery):
    def test_the_operator_file_wins_over_discovery(self):
        """A pin whose only remedy is editing the package is a pin someone
        eventually turns off."""
        home = self.tmp / "home"
        discovered = self._fake_binary(home / ".opencode" / "bin" / "opencode")
        elsewhere = self._fake_binary(self.tmp / "opt" / "opencode")
        cfg = self.tmp / "providers.json"
        cfg.write_text(json.dumps(
            {"opencode": {"path": str(elsewhere), "version": "9.9.9"}}))

        compat = self._reload(home=home, providers_file=cfg)
        self.assertEqual(compat.PINNED_PROVIDERS["opencode"]["path"], str(elsewhere))
        self.assertEqual(compat.PINNED_PROVIDERS["opencode"]["version"], "9.9.9")
        self.assertIn("9.9.9", compat.accepted_versions("opencode"))
        self.assertEqual(compat.pinned_for_path(str(elsewhere))[0], "opencode")
        self.assertIsNone(compat.pinned_for_path(str(discovered)),
                          "an overridden provider must not still match its "
                          "discovered path — that would widen what counts as live")

    def test_a_provider_this_build_has_no_rule_for_is_still_registered(self):
        """Silently dropping it would look exactly like a typo in the name."""
        cfg = self.tmp / "providers.json"
        cfg.write_text(json.dumps({"mistral": {"path": "/bin/echo", "version": "1.0"}}))
        compat = self._reload(providers_file=cfg)
        self.assertIn("mistral", compat.PINNED_PROVIDERS)
        self.assertEqual(compat.PINNED_PROVIDERS["mistral"]["path"], "/bin/echo")

    def test_a_broken_operator_file_does_not_break_discovery(self):
        cfg = self.tmp / "providers.json"
        cfg.write_text("{ not json")
        home = self.tmp / "home"
        self._fake_binary(home / ".opencode" / "bin" / "opencode")
        compat = self._reload(home=home, providers_file=cfg)
        self.assertIsNotNone(compat.PINNED_PROVIDERS["opencode"]["path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
