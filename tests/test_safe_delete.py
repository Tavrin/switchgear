#!/usr/bin/env python3
"""Recursive deletes must be guarded at the delete, not by the caller's care.

Every recursive delete in this rail interpolates a variable — a job's sandbox
home, a session store, a temp copy of a credential directory. Nothing prevented a
mistake except those variables happening to be correct every time, which is a run
of luck rather than a property. This is the same exposure another project was
pulled up on ("always be careful of what your rm -fr will do"), and the same
answer: the check lives at the delete.

Verified by EXECUTION, not by reading — including the empty-variable case, which
is the one that actually happens.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from ai_ops.errors import Refuse  # noqa: E402
from ai_ops.paths import safe_rmtree  # noqa: E402


class Refuses(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-rm-"))
        self.root = self.tmp / "root"
        (self.root / "child").mkdir(parents=True)
        (self.root / "child" / "f").write_text("x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_empty_variable_case(self):
        """The one that actually happens: an unset variable turns rmtree(home)
        into rmtree("")."""
        with self.assertRaises(Refuse):
            safe_rmtree("", must_be_under=str(self.root))

    def test_root_filesystem(self):
        with self.assertRaises(Refuse):
            safe_rmtree("/", must_be_under="/")

    def test_home_and_other_system_roots(self):
        for path in ("/home", "/etc", "/usr", "/var", "/tmp"):
            with self.assertRaises(Refuse, msg=path):
                safe_rmtree(path, must_be_under="/")

    def test_the_containing_root_itself(self):
        """A caller that means to empty a root must name its children. Deleting
        the root is how a state root or a sessions directory disappears."""
        with self.assertRaises(Refuse) as ctx:
            safe_rmtree(str(self.root), must_be_under=str(self.root))
        self.assertIn("IS the containing root", str(ctx.exception))
        self.assertTrue(self.root.exists())

    def test_a_path_outside_the_root(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        with self.assertRaises(Refuse) as ctx:
            safe_rmtree(str(outside), must_be_under=str(self.root))
        self.assertIn("outside", str(ctx.exception))
        self.assertTrue(outside.exists())

    def test_a_relative_path(self):
        with self.assertRaises(Refuse):
            safe_rmtree("child", must_be_under=str(self.root))

    def test_a_traversal_escape(self):
        with self.assertRaises(Refuse) as ctx:
            safe_rmtree(str(self.root / ".." / "elsewhere"),
                        must_be_under=str(self.root))
        self.assertIn("..", str(ctx.exception))

    def test_a_symlink_that_resolves_outside(self):
        """A symlinked component can be repointed between the check and the use,
        so the decision is made on the resolved location."""
        target = self.tmp / "precious"
        target.mkdir()
        (target / "keep").write_text("important")
        link = self.root / "sneaky"
        os.symlink(target, link)
        with self.assertRaises(Refuse):
            safe_rmtree(str(link), must_be_under=str(self.root))
        self.assertTrue((target / "keep").exists(), "the symlink target was deleted")

    def test_no_containing_root_given(self):
        with self.assertRaises(Refuse):
            safe_rmtree(str(self.root / "child"), must_be_under="")


class Permits(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-rm-ok-"))
        self.root = self.tmp / "root"
        (self.root / "child" / "deep").mkdir(parents=True)
        (self.root / "child" / "deep" / "f").write_text("x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_child_is_removed(self):
        self.assertTrue(safe_rmtree(str(self.root / "child"),
                                    must_be_under=str(self.root)))
        self.assertFalse((self.root / "child").exists())
        self.assertTrue(self.root.exists(), "the root must survive")

    def test_a_missing_path_reports_nothing_removed_rather_than_success(self):
        """A delete that quietly did nothing reads as success and hides a wrong
        path; the caller gets to know the difference."""
        self.assertFalse(safe_rmtree(str(self.root / "never-existed"),
                                     must_be_under=str(self.root)))

    def test_a_deep_child_is_removed(self):
        self.assertTrue(safe_rmtree(str(self.root / "child" / "deep"),
                                    must_be_under=str(self.root)))


class WiredIn(unittest.TestCase):
    """Every recursive delete in the rail must go through the guard. A new bare
    rmtree is exactly how this protection erodes."""

    def test_no_module_calls_rmtree_directly(self):
        allowed = {"paths.py"}  # the guard itself
        offenders = []
        for path in (ROOT / "python" / "ai_ops").glob("*.py"):
            if path.name in allowed:
                continue
            text = path.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "rmtree(" in stripped and "safe_rmtree(" not in stripped:
                    offenders.append(f"{path.name}:{i}")
        self.assertEqual(offenders, [],
                         f"bare rmtree outside the guard: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
