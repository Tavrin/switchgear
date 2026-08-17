#!/usr/bin/env python3
"""Hermetic adversarial suite. Provider is an absolute committed mock."""
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
MOCK = ROOT / "tests" / "helpers" / "mock_provider.py"
PYTHON = "/usr/bin/python3"
EXAMPLE = ROOT / "project-profiles" / "example.json"
MAKE_REPO = ROOT / "tests" / "helpers" / "make-synthetic-repo"


def run_cli(args, env=None, timeout=30):
    base = os.environ.copy()
    # Neutralize host secrets for the controller too where relevant
    for k in list(base):
        if k.startswith("OPENCODE_") or k in {"AI_OPS_ALLOW_LIVE_PROVIDER"}:
            if k != "AI_OPS_WRITE":
                base.pop(k, None)
    if env:
        base.update(env)
    proc = subprocess.run(
        [PYTHON, str(MAIN), *args],
        capture_output=True,
        text=True,
        env=base,
        timeout=timeout,
    )
    return proc


def write_profile(path: Path, **over):
    data = json.loads(EXAMPLE.read_text())
    data.update(over)
    path.write_text(json.dumps(data, indent=2) + "\n")


def envelope(cwd: str, role="implement", mode="bounded-write", **extra):
    obj = {
        "goal": "task",
        "context": "test",
        "constraints": [],
        "done_when": ["done"],
        "non_goals": [],
        "risk_threshold": "incorrect behavior only",
        "stop_condition": "stop",
        "expansion_rule": "wait",
        "mode": mode,
        "role": role,
        "cwd": cwd,
    }
    obj.update(extra)
    return obj


class RailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-"))
        self.state = self.tmp / "state"
        self.syn = self.tmp / "syn"
        self.profile = self.tmp / "profile.json"
        proc = subprocess.run(["bash", str(MAKE_REPO), str(self.syn)], check=True, capture_output=True, text=True)
        vals = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        self.primary = Path(vals["PRIMARY"])
        self.wt = Path(vals["WT"])
        self.wt2 = Path(vals["WT2"])
        self.sibling = Path(vals["SIBLING"])
        self.canary_s = Path(vals["CANARY_SIBLING"])
        self.canary_p = Path(vals["CANARY_PRIMARY"])
        write_profile(self.profile, write_enabled=True, commands={"probe": True})
        p = run_cli(["--state", str(self.state), "state", "provision", str(self.state)])
        self.assertEqual(p.returncode, 0, p.stderr)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, *rest):
        return [
            "--profile",
            str(self.profile),
            "--state",
            str(self.state),
            "--provider",
            str(MOCK),
            *rest,
        ]

    def test_schema_example_profile(self):
        from ai_ops.profile import load_profile

        sys.path.insert(0, str(ROOT / "python"))
        load_profile(str(EXAMPLE))

    def test_missing_provider_fails(self):
        p = run_cli(
            ["--profile", str(self.profile), "--state", str(self.state), "scout", str(self.primary), "x"]
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("provider path required", p.stderr)

    def test_missing_mock_fails(self):
        p = run_cli(self.args("scout", str(self.primary), "x"), env={"AI_OPS_PROVIDER": str(self.tmp / "nope")})
        # --provider still set to MOCK in args; use explicit missing
        p = run_cli(
            [
                "--profile",
                str(self.profile),
                "--state",
                str(self.state),
                "--provider",
                str(self.tmp / "missing-mock"),
                "scout",
                str(self.primary),
                "x",
            ]
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("missing", p.stderr)

    def test_live_provider_refused_without_allow(self):
        live = "/home/user/.opencode/bin/opencode"
        if not os.path.isfile(live):
            self.skipTest("live opencode absent")
        p = run_cli(
            [
                "--profile",
                str(self.profile),
                "--state",
                str(self.state),
                "--provider",
                live,
                "scout",
                str(self.primary),
                "x",
            ]
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("AI_OPS_ALLOW_LIVE_PROVIDER", p.stderr)

    def test_models(self):
        p = run_cli(self.args("models"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("deepseek-v4-flash", p.stdout)
        self.assertIn("family=deepseek", p.stdout)

    def test_readonly_ok(self):
        p = run_cli(self.args("scout", str(self.primary), "look"), env={"AI_OPS_MOCK_BEHAVIOR": "ok"})
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("job=", p.stdout)

    def test_readonly_mutation_impossible(self):
        before = (self.primary / "README.md").read_text()
        p = run_cli(self.args("scout", str(self.primary), "x"), env={"AI_OPS_MOCK_BEHAVIOR": "edit-tracked"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.primary / "README.md").read_text(), before)

    def test_hostile_permission_not_forwarded(self):
        p = run_cli(
            self.args("scout", str(self.primary), "x"),
            env={
                "AI_OPS_MOCK_BEHAVIOR": "dump-env",
                "OPENCODE_PERMISSION": '{"bash":"allow","edit":"allow"}',
                "GIT_DIR": "/tmp/evil.git",
                "LD_PRELOAD": "/tmp/evil.so",
                "PYTHONPATH": "/tmp/evilpy",
            },
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        job = [ln for ln in p.stdout.splitlines() if ln.startswith("job=")][0].split("=", 1)[1]
        ev = (self.state / "jobs" / job / "evidence" / "events.jsonl").read_text()
        data = json.loads(ev.splitlines()[0])
        env = data["env"]
        self.assertIsNone(env.get("OPENCODE_PERMISSION"))
        self.assertIsNone(env.get("GIT_DIR"))
        self.assertIsNone(env.get("LD_PRELOAD"))
        self.assertTrue(env["HOME"].endswith("sandbox-home") or "sandbox-home" in env["HOME"])
        self.assertEqual(env.get("OPENCODE_DISABLE_PROJECT_CONFIG"), "1")

    def test_state_symlink_into_git_refused(self):
        evil = self.tmp / "evilstate"
        evil.mkdir()
        (evil / ".ai-ops-state").write_text("x\n")
        (evil / "jobs").symlink_to(self.primary / ".git")
        p = run_cli(
            [
                "--profile",
                str(self.profile),
                "--state",
                str(evil),
                "--provider",
                str(MOCK),
                "scout",
                str(self.primary),
                "x",
            ]
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("symlink", p.stderr.lower())

    def test_write_kill_switch(self):
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(self.args("write", str(self.wt), "implement", "--envelope", str(envf)))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("AI_OPS_WRITE", p.stderr)

    def test_write_example_disabled(self):
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            [
                "--profile",
                str(EXAMPLE),
                "--state",
                str(self.state),
                "--provider",
                str(MOCK),
                "write",
                str(self.wt),
                "implement",
                "--envelope",
                str(envf),
            ],
            env={"AI_OPS_WRITE": "1"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("write_enabled", p.stderr)

    def test_write_primary_refused(self):
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.primary))))
        p = run_cli(
            self.args("write", str(self.primary), "implement", "--envelope", str(envf)),
            env={"AI_OPS_WRITE": "1"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("linked worktree", p.stderr)

    def test_write_requires_lease(self):
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf)),
            env={"AI_OPS_WRITE": "1"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no lease", p.stderr)

    def _acquire(self):
        p = run_cli(self.args("lease", "acquire", "--dir", str(self.wt), "--owner", "t"))
        self.assertEqual(p.returncode, 0, p.stderr)
        return [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("lease=")][0]

    def test_write_inside_and_review_promote(self):
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt), commands=[{"verb": "probe", "args": []}])))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")
        self.assertIn("worker-edit", (self.wt / "tracked.txt").read_text())
        # sibling canary untouched
        self.assertEqual(self.canary_s.read_text(), "sibling-canary\n")
        # review promote
        rev_env = envelope(str(self.wt), role="review", mode="readonly", parent_job=job)
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(rev_env))
        p = run_cli(
            self.args("review", str(self.wt), "review", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},
        )
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "ok")

    def test_review_reject_cannot_promote(self):
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(envelope(str(self.wt), role="review", mode="readonly", parent_job=job)))
        p = run_cli(
            self.args("review", str(self.wt), "review", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-reject"},
        )
        self.assertNotEqual(p.returncode, 0)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_empty_review_cannot_promote(self):
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(envelope(str(self.wt), role="review", mode="readonly", parent_job=job)))
        p = run_cli(
            self.args("review", str(self.wt), "review", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-empty"},
        )
        self.assertNotEqual(p.returncode, 0)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_same_family_required_refuses(self):
        data = json.loads(self.profile.read_text())
        data["models"]["allow"].append("opencode-go/glm-5.2")
        data["roles"]["review2"] = {"model": "opencode-go/glm-5.2", "mode": "readonly"}
        data["review"]["independence"]["different_family"] = "required"
        self.profile.write_text(json.dumps(data, indent=2))
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(envelope(str(self.wt), role="review2", mode="readonly", parent_job=job)))
        p = run_cli(
            self.args("review", str(self.wt), "review2", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},
        )
        self.assertNotEqual(p.returncode, 0)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_escape_outside_cannot_write_host(self):
        token = self._acquire()
        before = self.canary_s.read_text()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={
                "AI_OPS_WRITE": "1",
                "AI_OPS_MOCK_BEHAVIOR": "edit-outside",
                "AI_OPS_MOCK_EXTRA": str(self.canary_s),
            },
        )
        # job may complete with handoff but host canary must be unchanged
        self.assertEqual(self.canary_s.read_text(), before)
        self.assertEqual(self.canary_p.read_text(), "primary-canary\n")

    def test_forged_lease_token(self):
        self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args(
                "write",
                str(self.wt),
                "implement",
                "--envelope",
                str(envf),
                "--token",
                "00000000-0000-0000-0000-000000000000",
            ),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("forged", p.stderr)

    def test_simultaneous_workers(self):
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        env = os.environ.copy()
        env.update(
            {
                "AI_OPS_WRITE": "1",
                "AI_OPS_MOCK_BEHAVIOR": "hang",
                "AI_OPS_PROVIDER": str(MOCK),
            }
        )
        wargs = self.args(
            "write", str(self.wt), "implement", "--envelope", str(envf), "--token", token
        )
        a = subprocess.Popen(
            [PYTHON, str(MAIN), *wargs],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.4)
        b = subprocess.run(
            [PYTHON, str(MAIN), *wargs],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        a.kill()
        a.wait(timeout=5)
        self.assertNotEqual(b.returncode, 0)
        self.assertIn("another worker", b.stderr)

    def test_events_malformed(self):
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "malformed"},
        )
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "provider_error")

    def test_events_truncated_plain_dup_nohandoff(self):
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        for beh in ("truncated", "plain-text", "prefix-garbage", "duplicate", "no-handoff", "wrong-handoff"):
            p = run_cli(
                self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
                env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": beh},
            )
            job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
            st = json.loads((self.state / "jobs" / job / "result.json").read_text())
            self.assertEqual(st["status"], "provider_error", beh)

    def test_command_shell_refused(self):
        from ai_ops.commands import resolve_command
        from ai_ops.errors import Refuse

        sys.path.insert(0, str(ROOT / "python"))
        with self.assertRaises(Refuse):
            resolve_command("nosuch", [])

    def test_string_envelope_commands_rejected(self):
        from ai_ops.schema import validate
        from ai_ops.errors import Refuse

        sys.path.insert(0, str(ROOT / "python"))
        bad = envelope(str(self.wt), commands=["cargo test"])
        with self.assertRaises(Refuse):
            validate(bad, "task-envelope.schema.json")

    def test_unknown_role(self):
        p = run_cli(self.args("review", str(self.primary), "nosuch", "x"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("unknown role", p.stderr)

    def test_timeout_zero_refused(self):
        p = run_cli(
            self.args("scout", str(self.primary), "x"),
            env={"AI_OPENCODE_TIMEOUT": "0", "AI_OPS_MOCK_BEHAVIOR": "ok"},
        )
        self.assertNotEqual(p.returncode, 0)

    def test_git_dir_env_cannot_redirect_identity(self):
        p = run_cli(
            self.args("scout", str(self.primary), "x"),
            env={"GIT_DIR": "/tmp/does-not-exist-git", "AI_OPS_MOCK_BEHAVIOR": "ok"},
        )
        self.assertEqual(p.returncode, 0, p.stderr)

    # ---- N-series regressions (see docs/REVIEW-6d217a6.md) ----

    def _subject_awaiting_review(self):
        """A bounded-write job parked in awaiting_review on self.wt."""
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")
        return job

    def test_n1_review_of_other_worktree_cannot_promote(self):
        """N1 (CRITICAL): a reviewer that never inspected the subject must not promote it.

        Regression guard for the freeze-compared-to-itself defect: promotion used
        to copy subject_tree_digest out of the subject's own freeze and compare it
        back, so a review of an unrelated worktree promoted the subject.
        """
        job = self._subject_awaiting_review()
        # Reviewer runs against a DIFFERENT worktree and votes promote.
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(envelope(str(self.wt2), role="review", mode="readonly", parent_job=job)))
        p = run_cli(
            self.args("review", str(self.wt2), "review", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},
        )
        self.assertNotEqual(p.returncode, 0, "review of another worktree must not promote")
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_n1_promote_cli_rejects_unrelated_review_job(self):
        """N1: the promote subcommand must also refuse a review job from elsewhere."""
        job = self._subject_awaiting_review()
        p = run_cli(
            self.args("review", str(self.wt2), "review", "x"),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        rev = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        p = run_cli(self.args("promote", "--subject", job, "--review", rev))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("did not inspect the subject worktree", p.stderr + p.stdout)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_n1_subject_cannot_review_itself(self):
        """N1: the subject's own job record must never serve as its review."""
        job = self._subject_awaiting_review()
        p = run_cli(self.args("promote", "--subject", job, "--review", job))
        self.assertNotEqual(p.returncode, 0)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_n1_matching_review_still_promotes(self):
        """The binding must not be so strict that a legitimate review fails."""
        job = self._subject_awaiting_review()
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(envelope(str(self.wt), role="review", mode="readonly", parent_job=job)))
        p = run_cli(
            self.args("review", str(self.wt), "review", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},
        )
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["review"]["reviewed_tree_digest"], st["freeze"]["tree_digest"])

    def test_n4_nonzero_write_exit_is_provider_error(self):
        """N4: a bounded-write provider that crashes must not reach awaiting_review."""
        token = self._acquire()
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf), "--token", token),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "exit-nonzero"},
        )
        self.assertNotEqual(p.returncode, 0)
        job = [ln.split("=", 1)[1] for ln in p.stdout.splitlines() if ln.startswith("job=")][0]
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "provider_error")

    def test_n6_write_requires_presented_token(self):
        """N6: the lease token is an authorization factor, not read off disk."""
        self._acquire()  # a lease exists, but we present no token
        envf = self.tmp / "e.json"
        envf.write_text(json.dumps(envelope(str(self.wt))))
        p = run_cli(
            self.args("write", str(self.wt), "implement", "--envelope", str(envf)),
            env={"AI_OPS_WRITE": "1", "AI_OPS_MOCK_BEHAVIOR": "edit-inside"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("lease token", p.stderr)

    def test_n7_non_numeric_timeout_refused(self):
        p = run_cli(
            self.args("scout", str(self.primary), "x"),
            env={"AI_OPENCODE_TIMEOUT": "abc", "AI_OPS_MOCK_BEHAVIOR": "ok"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("AI_OPENCODE_TIMEOUT", p.stderr)

    # ---- F-series regressions (second adversarial review) ----

    def test_f2_noop_reviewer_cannot_promote(self):
        """F2: a reviewer that inspected nothing must not promote, even on the
        correct worktree. Controller-side hashing is not inspection evidence."""
        job = self._subject_awaiting_review()
        rf = self.tmp / "r.json"
        rf.write_text(json.dumps(envelope(str(self.wt), role="review", mode="readonly", parent_job=job)))
        p = run_cli(
            self.args("review", str(self.wt), "review", "--envelope", str(rf)),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote-noop"},
        )
        self.assertNotEqual(p.returncode, 0)
        st = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertEqual(st["status"], "awaiting_review")

    def test_f3_repo_config_cannot_execute_on_host(self):
        """F3: a repository-owned diff.external must not run during tree_digest."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        marker = self.tmp / "F3_MARKER"
        evil = self.wt / "evil.sh"
        evil.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 0\n")
        evil.chmod(0o755)
        subprocess.run(
            ["/usr/bin/git", "-C", str(self.wt), "config", "diff.external", str(evil)],
            check=True, capture_output=True,
        )
        (self.wt / "tracked.txt").write_text("changed-for-diff\n")
        ident = identity.inspect_worktree(str(self.wt))
        identity.tree_digest(ident)
        self.assertFalse(marker.exists(), "repo-configured diff.external executed on the host")

    def test_f4_state_root_inside_worktree_refused(self):
        """F4: state inside the target would nest a writable bind in a --ro-bind."""
        inner = self.wt / ".ai-ops-state"
        p = run_cli(["--state", str(inner), "state", "provision", str(inner)])
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run_cli(
            [
                "--profile", str(self.profile), "--state", str(inner),
                "--provider", str(MOCK), "scout", str(self.wt), "x",
            ],
            env={"AI_OPS_MOCK_BEHAVIOR": "ok"},
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("overlap", p.stderr)

    def test_f6_symlinked_job_dir_refused(self):
        """F6: a symlinked jobs/<uuid> must not redirect state reads."""
        outside = self.tmp / "outside"
        uuid = "11111111-2222-3333-4444-555555555555"
        (outside / uuid).mkdir(parents=True)
        (outside / uuid / "result.json").write_text(json.dumps({
            "job_id": uuid, "status": "ok", "mode": "readonly", "role": "review",
            "model": {"id": "x"}, "dir": "/tmp", "exit": 0, "started": "s", "generation": 0,
        }))
        (self.state / "jobs" / uuid).symlink_to(outside / uuid)
        p = run_cli(self.args("status", uuid))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("symlink", p.stderr.lower())

    def test_f7_unhashable_event_type_is_provider_error(self):
        """F7: {"type": []} must not crash the controller with a TypeError."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.errors import ProviderError
        from ai_ops.events import parse_event_stream

        with self.assertRaises(ProviderError):
            parse_event_stream(b'{"type":[]}\n', require_handoff=False)

    def test_no_host_api_keys_can_reach_a_provider(self):
        """The provider environment is BUILT, not filtered. Nothing resembling a
        host credential may appear in it, whatever is set on the host."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.env import allowlisted_env

        hostile = {
            "OPENAI_API_KEY": "sk-should-never-appear",
            "AZURE_OPENAI_API_KEY": "x",
            "ANTHROPIC_API_KEY": "x",
            "GEMINI_API_KEY": "x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "GITHUB_TOKEN": "x",
        }
        saved = {k: os.environ.get(k) for k in hostile}
        os.environ.update(hostile)
        try:
            env = allowlisted_env(home="/tmp/probe-home")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        # Exactly one credential may ever be forwarded, and only deliberately
        # (provider.CREDENTIAL_ENV, injected from an operator-owned file under an
        # explicit live opt-in). Anything else credential-shaped is a leak.
        from ai_ops.provider import CREDENTIAL_ENV

        leaked = [
            k for k in env
            if k.endswith(("_API_KEY", "_TOKEN", "_SECRET")) and k != CREDENTIAL_ENV
        ]
        self.assertEqual(leaked, [], f"credential-shaped vars leaked: {leaked}")
        for k, v in env.items():
            self.assertNotIn("sk-should-never-appear", str(v), f"{k} carries a host secret")
        for k in hostile:
            self.assertNotIn(k, env)

    def test_provider_credential_never_read_from_host_env(self):
        """The one forwarded credential comes from an operator-owned file, never
        from the controller's environment (where unrelated secrets live)."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.provider import CREDENTIAL_ENV, load_provider_credential

        missing = self.tmp / "no-such-credential"
        os.environ["AI_OPS_PROVIDER_CREDENTIAL_FILE"] = str(missing)
        os.environ[CREDENTIAL_ENV] = "host-env-value-must-be-ignored"
        try:
            self.assertIsNone(load_provider_credential())
        finally:
            os.environ.pop("AI_OPS_PROVIDER_CREDENTIAL_FILE", None)
            os.environ.pop(CREDENTIAL_ENV, None)

    def test_provider_credential_file_must_not_be_world_readable(self):
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.errors import Refuse
        from ai_ops.provider import load_provider_credential

        cred = self.tmp / "cred"
        cred.write_text("secret\n")
        cred.chmod(0o644)
        os.environ["AI_OPS_PROVIDER_CREDENTIAL_FILE"] = str(cred)
        try:
            with self.assertRaises(Refuse):
                load_provider_credential()
            cred.chmod(0o600)
            self.assertEqual(load_provider_credential(), "secret")
        finally:
            os.environ.pop("AI_OPS_PROVIDER_CREDENTIAL_FILE", None)

    def test_k1_untracked_content_is_in_the_digest(self):
        """kimi-1 (BLOCKER): a worker cannot stage (git dir is ro in-sandbox), so
        every file it creates is untracked. `git status` reports untracked files
        by NAME only and `git diff HEAD` covers tracked content only, so the
        freeze/review/'changed after review' checks were blind to their content."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        ident = identity.inspect_worktree(str(self.wt))
        (self.wt / "app.py").write_text("benign\n")
        before = identity.tree_digest(ident)
        (self.wt / "app.py").write_text("BACKDOOR\n")
        after = identity.tree_digest(ident)
        self.assertNotEqual(before, after, "untracked content must change the digest")

    def test_k1_gitignored_content_cannot_hide(self):
        """kimi-1b: .gitignore is worker-writable; ignored files must not vanish."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        ident = identity.inspect_worktree(str(self.wt))
        (self.wt / ".gitignore").write_text("loot/\n")
        (self.wt / "loot").mkdir(exist_ok=True)
        (self.wt / "loot" / "secret").write_text("a\n")
        before = identity.tree_digest(ident)
        self.assertIn("loot/secret", identity.changed_files(ident))
        (self.wt / "loot" / "secret").write_text("exfiltrated\n")
        self.assertNotEqual(before, identity.tree_digest(ident))

    def test_k2_pointer_to_common_gitdir_refused(self):
        """kimi-2 (HIGH): containment is not ownership. git_dir == common passed
        the containment checks, so a pointer at the PRIMARY's git dir was adopted
        and then self-ratified forever."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity
        from ai_ops.errors import Refuse

        original = (self.wt / ".git").read_text()
        (self.wt / ".git").write_text(f"gitdir: {self.primary}/.git\n")
        try:
            with self.assertRaises(Refuse):
                identity.inspect_worktree(str(self.wt))
        finally:
            (self.wt / ".git").write_text(original)
        self.assertTrue(identity.inspect_worktree(str(self.wt)).linked_worktree)

    def test_k5_review_binds_to_the_subjects_own_change(self):
        """kimi-5 / dogfood D2: the worktree persists across jobs, so comparing
        against the LIVE cumulative delta credited one job with another's work."""
        job = self._subject_awaiting_review()
        rec = json.loads((self.state / "jobs" / job / "result.json").read_text())
        self.assertIn("changed_files", rec["freeze"])
        self.assertIn("tracked.txt", rec["freeze"]["changed_files"])

    def test_d1_poisoned_gitdir_pointer_refused_across_jobs(self):
        """deepseek-F1 (BLOCKER): a .git pointer redirected by an earlier job
        PERSISTS on disk even though that job refused. A later job must not adopt
        a worker-owned repository as the authoritative git dir -- otherwise every
        digest, freeze and promotion comparison is computed against it."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity
        from ai_ops.errors import Refuse

        evil = self.wt / "evilrepo"
        evil.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "a@b"],
                     ["config", "user.name", "a"]):
            subprocess.run(["/usr/bin/git", "-C", str(evil), *args], check=True, capture_output=True)
        (evil / "f").write_text("x")
        subprocess.run(["/usr/bin/git", "-C", str(evil), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["/usr/bin/git", "-C", str(evil), "commit", "-qm", "x"], check=True, capture_output=True)
        original = (self.wt / ".git").read_text()
        (self.wt / ".git").write_text(f"gitdir: {evil}/.git\n")
        try:
            with self.assertRaises(Refuse):
                identity.inspect_worktree(str(self.wt))
        finally:
            (self.wt / ".git").write_text(original)
        # and the legitimate pointer still works
        ident = identity.inspect_worktree(str(self.wt))
        self.assertTrue(ident.linked_worktree)

    def test_d1_primary_gitdir_must_be_dot_git(self):
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        ident = identity.inspect_worktree(str(self.primary))
        self.assertFalse(ident.linked_worktree)
        self.assertEqual(
            os.path.realpath(ident.git_dir),
            os.path.realpath(str(self.primary / ".git")),
        )

    def test_d_note_host_secret_check_raises_refuse(self):
        """deepseek note: bare RuntimeError is not caught by cli.main."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.env import assert_no_host_secrets
        from ai_ops.errors import Refuse

        with self.assertRaises(Refuse):
            assert_no_host_secrets({"OPENCODE_PERMISSION": '{"bash":"allow"}'})

    def test_g4_invalid_utf8_is_provider_error(self):
        """glm-F4: invalid UTF-8 raised UnicodeDecodeError (a ValueError), which
        the rail's (ProviderError, Refuse) handler does not catch."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.errors import ProviderError
        from ai_ops.events import parse_event_stream

        with self.assertRaises(ProviderError):
            parse_event_stream(b"\xff\xfe\x00bad", require_handoff=False)

    def test_g6_host_side_version_check_is_gone(self):
        """glm-F6: the orphaned host-exec version check must not be reintroduced."""
        src = (ROOT / "python" / "ai_ops" / "compat.py").read_text()
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("import subprocess", code)
        self.assertNotIn("subprocess.run", code)
        self.assertNotIn("def check_opencode_version", code)

    def test_g7_evidence_survives_failed_integrity_assert(self):
        """glm-F7: a worker that trips a post-run assert must not erase its own
        evidence -- events.jsonl is written before the asserts run."""
        job_src = (ROOT / "python" / "ai_ops" / "job.py").read_text()
        ev = job_src.index('ev_path = os.path.join(dirs["evidence"]')
        assert_at = job_src.index("identity.assert_gitdir_pointer_intact(ident)")
        self.assertLess(ev, assert_at, "evidence must be persisted before integrity asserts")

    def test_n2_legacy_unsandboxed_binaries_are_gone(self):
        """N2: the pre-Python host-side command path must not be shipped."""
        for stale in ("bin/ai-cmd", "bin/ai-ro", "lib/common.sh", "lib/policy.sh"):
            self.assertFalse((ROOT / stale).exists(), f"{stale} must not be reintroduced")


class PathUnit(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-paths-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_n8_symlink_after_missing_component_is_detected(self):
        """N8: the walker used to stop at the first missing component and report clean."""
        from ai_ops.paths import _symlink_in_path

        link = self.tmp / "link"
        link.symlink_to("/etc")
        # A missing component precedes the symlink in the walk order.
        probe = str(self.tmp / "link" / "deep" / "leaf")
        self.assertEqual(_symlink_in_path(probe), str(link))


class EventUnit(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def test_parse_ok(self):
        from ai_ops.events import parse_event_stream

        raw = b'{"type":"complete","handoff":{"summary":"s","status":"awaiting_review"}}\n'
        parse_event_stream(raw, require_handoff=True)

    def test_parse_garbage_tail(self):
        from ai_ops.events import parse_event_stream
        from ai_ops.errors import ProviderError

        raw = b'{"type":"complete"}\nGARBAGE'
        with self.assertRaises(ProviderError):
            parse_event_stream(raw, require_handoff=False)


class BrokerUnit(unittest.TestCase):
    """Credential broker + no-network sandbox."""

    primary = None

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))
        if BrokerUnit.primary is None:
            tmp = Path(tempfile.mkdtemp(prefix="aiops-broker-"))
            proc = subprocess.run(["bash", str(MAKE_REPO), str(tmp / "syn")],
                                  check=True, capture_output=True, text=True)
            vals = dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)
            BrokerUnit.primary = Path(vals["PRIMARY"])

    def test_sandbox_argv_unshares_net_only_with_a_broker(self):
        from ai_ops import identity, sandbox
        from ai_ops.policy import compile_policy
        from ai_ops.profile import load_profile

        pol = compile_policy(load_profile(str(EXAMPLE)), "readonly")
        ident = identity.inspect_worktree(str(self.__class__.primary))
        without = sandbox.build_bwrap_argv(
            ident=ident, policy=pol, synth_home="/tmp", provider_argv=["/x"]
        )
        self.assertNotIn("--unshare-net", without)
        with_broker = sandbox.build_bwrap_argv(
            ident=ident, policy=pol, synth_home="/tmp", provider_argv=["/x"],
            broker_socket="/tmp/fake.sock",
        )
        self.assertIn("--unshare-net", with_broker)
        self.assertIn(sandbox.BROKER_SOCKET_PATH, with_broker)

    def test_credential_never_appears_in_the_runtime_config(self):
        from ai_ops.provider import runtime_with_broker

        rt = runtime_with_broker({"tools": {}}, "http://127.0.0.1:8099", "opencode-go/glm-5.3")
        blob = json.dumps(rt)
        self.assertIn("broker-placeholder-not-a-credential", blob)
        self.assertIn("127.0.0.1:8099", blob)
        opts = rt["provider"]["opencode-go"]["options"]
        self.assertNotIn("REAL", opts["apiKey"].upper())

    def test_broker_pins_model_and_path(self):
        import urllib.error
        import urllib.request

        from ai_ops.broker import CredentialBroker

        with CredentialBroker("SECRET", upstream="http://127.0.0.1:9/v1",
                              allowed_models={"opencode-go/glm-5.3"}) as bk:
            def post(path, model):
                req = urllib.request.Request(
                    bk.base_url + path,
                    data=json.dumps({"model": model}).encode(),
                    headers={"content-type": "application/json"}, method="POST")
                try:
                    urllib.request.urlopen(req, timeout=5)
                    return 200
                except urllib.error.HTTPError as exc:
                    return exc.code
                except Exception:
                    return 0
            self.assertEqual(post("/v1/embeddings", "opencode-go/glm-5.3"), 403)
            self.assertEqual(post("/v1/chat/completions", "openai/gpt-4"), 403)
            self.assertGreaterEqual(len(bk.denials), 2)


class MultiProviderUnit(unittest.TestCase):
    """OpenRouter/multi-provider routing (agents, reviewers, swarms)."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def test_provider_routing_per_model(self):
        from ai_ops.registry import model_record, provider_record

        go = model_record("opencode-go/glm-5.3")
        orr = model_record("openrouter/anthropic/claude-sonnet-4.5")
        self.assertEqual(go["provider"], "opencode-go")
        self.assertEqual(orr["provider"], "openrouter")
        self.assertNotEqual(
            provider_record(go["provider"])["upstream"],
            provider_record(orr["provider"])["upstream"],
        )

    def test_unknown_provider_refused(self):
        from ai_ops.errors import Refuse
        from ai_ops.registry import provider_record

        with self.assertRaises(Refuse):
            provider_record("not-a-provider")

    def test_broker_model_pin_accepts_either_wire_form(self):
        """A provider may send the full id or the provider-stripped suffix."""
        from ai_ops.registry import wire_model_names

        names = wire_model_names("openrouter/anthropic/claude-sonnet-4.5")
        self.assertIn("openrouter/anthropic/claude-sonnet-4.5", names)
        self.assertIn("anthropic/claude-sonnet-4.5", names)
        self.assertNotIn("anthropic/claude-opus-4", names)

    def test_cross_vendor_independence_is_expressible(self):
        """The point of OpenRouter here: reviewers from a different vendor.

        different_family alone is weak -- two models can share a vendor. The
        registry carries vendor_family so a profile can demand real diversity.
        """
        from ai_ops.registry import model_record
        from ai_ops.review import independence

        subject = model_record("opencode-go/deepseek-v4-pro")
        same_vendor = model_record("openrouter/deepseek/deepseek-chat")
        cross_vendor = model_record("openrouter/anthropic/claude-sonnet-4.5")

        self.assertEqual(subject["vendor_family"], same_vendor["vendor_family"])
        self.assertNotEqual(subject["vendor_family"], cross_vendor["vendor_family"])
        ind = independence(subject, cross_vendor, "a", "b")
        self.assertTrue(ind["different_model"])
        self.assertTrue(ind["different_family"])

    def test_credentials_are_per_provider(self):
        from ai_ops.provider import credential_path

        os.environ.pop("AI_OPS_PROVIDER_CREDENTIAL_FILE", None)
        self.assertTrue(credential_path("openrouter").endswith("provider-credential")
                        or "credentials/openrouter" in credential_path("openrouter"))
        os.environ["AI_OPS_PROVIDER_CREDENTIAL_FILE"] = "/tmp/override-cred"
        try:
            self.assertEqual(credential_path("openrouter"), "/tmp/override-cred")
        finally:
            os.environ.pop("AI_OPS_PROVIDER_CREDENTIAL_FILE", None)





if __name__ == "__main__":
    unittest.main()
