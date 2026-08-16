#!/usr/bin/env python3
"""No-model OpenCode config-precedence probe. Does not call a model."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_ops.compat import PINNED_BINARY, PINNED_OPENCODE
from ai_ops.provider import isolation_env
from ai_ops.sandbox import TRUSTED_BWRAP, require_bwrap

ROOT = Path(__file__).resolve().parents[1]


class ConfigProbe(unittest.TestCase):
    def test_pinned_version_or_skip(self):
        if not os.path.isfile(PINNED_BINARY):
            self.skipTest("live OpenCode not installed")
        proc = subprocess.run([PINNED_BINARY, "--version"], capture_output=True, text=True)
        ver = (proc.stdout or "").strip().splitlines()[-1].strip()
        self.assertEqual(ver, PINNED_OPENCODE)

    def test_isolated_env_omits_permission(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = {
                "tools": {"bash": False, "edit": False},
                "permission": {"bash": "deny", "edit": "deny"},
            }
            os.environ["OPENCODE_PERMISSION"] = '{"bash":"allow"}'
            try:
                env = isolation_env(td, runtime)
            finally:
                os.environ.pop("OPENCODE_PERMISSION", None)
            self.assertNotIn("OPENCODE_PERMISSION", env)
            self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
            self.assertTrue(env["OPENCODE_CONFIG"].startswith(td))

    def test_opencode_models_under_isolation_no_host_permission(self):
        if not os.path.isfile(PINNED_BINARY):
            self.skipTest("live OpenCode not installed")
        require_bwrap()
        with tempfile.TemporaryDirectory() as td:
            runtime = {"tools": {"bash": False}, "permission": {"bash": "deny"}}
            env = isolation_env(td, runtime)
            # dump env via bwrap+env; do not pass a prompt (no model)
            argv = [TRUSTED_BWRAP, "--unshare-pid", "--die-with-parent"]
            for src, dst in (("/usr", "/usr"), ("/lib", "/lib"), ("/lib64", "/lib64")):
                if os.path.exists(src):
                    argv.extend(["--ro-bind", src, dst])
            argv.extend(
                [
                    "--dev",
                    "/dev",
                    "--proc",
                    "/proc",
                    "--bind",
                    td,
                    td,
                    "--setenv",
                    "HOME",
                    td,
                    "--",
                    "/usr/bin/env",
                ]
            )
            proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("OPENCODE_PERMISSION=", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
