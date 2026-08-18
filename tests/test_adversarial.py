#!/usr/bin/env python3
"""Hermetic adversarial suite. Provider is an absolute committed mock."""
from __future__ import annotations

import json
import os
import shutil
import signal
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

    def test_standalone_review_persists_its_verdict_on_its_own_record(self):
        """Bug #3: a review with no parent left review:null on disk. The verdict
        lived only in events.jsonl and was lost once the sandbox home was
        reclaimed (workaround: AI_OPS_KEEP_SANDBOX_HOME). It must land on the
        reviewer's own result record, home purged or not."""
        (self.primary / "app.py").write_text("changed = 1\n")
        p = run_cli(
            self.args("--json", "review", str(self.primary), "review", "Review this"),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},  # no KEEP_SANDBOX_HOME
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        job_id = json.loads(p.stdout)["job_id"]
        home = self.state / "jobs" / job_id / "sandbox-home"
        self.assertFalse(home.exists(), "home should be reclaimed by default")
        rec = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        self.assertIsNotNone(rec["review"], "verdict must be persisted, not null")
        self.assertEqual(rec["review"]["verdict"], "promote")
        self.assertIn("app.py", rec["review"]["reviewed_files"])

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

    def _launch(self, extra="6"):
        p = run_cli(
            self.args("--json", "scout", str(self.primary), "look", "--background"),
            env={"AI_OPS_MOCK_BEHAVIOR": "slow-stream", "AI_OPS_MOCK_EXTRA": extra},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def _state_of(self, job_id):
        p = run_cli(self.args("--json", "status", job_id))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["state"]

    def test_an_unreadable_file_does_not_crash_the_audit(self):
        """Bug #2, found dogfooding on a large private repository: a mode-000 / root-owned file
        (a meilisearch data dir) crashed tree_digest with PermissionError, making
        the whole platform unauditable. It must fingerprint from metadata under
        an UNREADABLE marker instead -- never crash, never silently vanish."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        secret = self.primary / "unreadable.bin"
        secret.write_text("data")
        os.chmod(secret, 0o000)
        try:
            digest = identity.tree_digest(identity.inspect_worktree(str(self.primary)))
            self.assertTrue(digest)
            # And the digest is sensitive to a metadata change on that file.
            os.chmod(secret, 0o004)
            digest2 = identity.tree_digest(identity.inspect_worktree(str(self.primary)))
            self.assertNotEqual(digest, digest2, "mode change on unreadable file must move digest")
        finally:
            os.chmod(secret, 0o644)

    def test_a_new_file_is_visible_to_the_reviewer(self):
        """`git diff HEAD` is tracked-only, so a NEW file appears in the changed
        list with no content behind it. Found by a live reviewer on a real repo:
        it reported it could not verify a security-sensitive new config file
        because no hunk was provided. For bounded-write this is the COMMON case
        -- a worker cannot stage, so everything it creates is untracked, and the
        most important changes would be reviewed blind."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        (self.primary / "brand_new.py").write_text("PAYLOAD = 'must be reviewable'\n")
        ident = identity.inspect_worktree(str(self.primary))
        tracked_only = identity.worktree_diff(ident)
        self.assertNotIn("PAYLOAD", tracked_only, "fixture assumption: not in git diff HEAD")

        changed, _ = identity.review_manifest(ident)
        self.assertIn("brand_new.py", changed)
        extra = identity.untracked_diff(ident, ["brand_new.py"])
        self.assertIn("PAYLOAD", extra)
        self.assertIn("new file", extra)

    def test_new_file_diffs_are_bounded(self):
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        (self.primary / "big_new.txt").write_text("A" * 50_000)
        ident = identity.inspect_worktree(str(self.primary))
        out = identity.untracked_diff(ident, ["big_new.txt"], max_bytes=5_000)
        self.assertLessEqual(len(out), 6_000)
        self.assertIn("truncated", out)

    def test_review_manifest_separates_change_from_ambient_ignored(self):
        """Bug #4: a populated .venv put 42,205 paths (3.7MB) in the review
        attachment and a reviewer burned its timeout on ambient noise. The
        integrity digest must still cover everything (anti-hiding), but the
        review manifest must surface only the real change."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity

        (self.primary / "real_change.py").write_text("x = 1\n")  # new source
        (self.primary / ".gitignore").write_text(".venv/\n")
        venv = self.primary / ".venv" / "lib"
        venv.mkdir(parents=True)
        for n in range(200):
            (venv / f"f{n}.py").write_text(f"x={n}\n")

        ident = identity.inspect_worktree(str(self.primary))
        # Integrity still sees all of it: anti-hiding intact.
        self.assertGreater(len(identity.changed_files(ident)), 200)
        changed, ignored = identity.review_manifest(ident)
        # Review sees the real change, not the 200 ambient files.
        self.assertIn("real_change.py", changed)
        self.assertGreaterEqual(len(ignored), 200)
        self.assertNotIn(".venv/lib/f0.py", changed)
        self.assertTrue(all(".venv" not in c for c in changed))

    def test_a_large_diff_does_not_blow_the_kernel_argv_limit(self):
        """Found in real use on a large private repository, 2026-08-18.

        The review verb concatenated the whole uncommitted diff into the prompt,
        which is ONE element of argv. Linux caps a single argument at
        MAX_ARG_STRLEN (128KiB), while worktree_diff caps at 200KB -- so the cap
        itself guaranteed that any repo with a real uncommitted change died at
        exec with `[Errno 7] Argument list too long` before the provider even
        started. A clean fixture worktree has no diff, which is why the whole
        hermetic suite passed over it.

        The diff is now attached as a file. This test asserts the argv path stays
        bounded no matter how large the change is.
        """
        big = "\n".join(f"line_{i} = {i}" for i in range(6000))
        (self.primary / "big.py").write_text(big)
        subprocess.run(["git", "-C", str(self.primary), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.primary), "-c", "user.email=a@b",
                        "-c", "user.name=a", "commit", "-qm", "big"], check=True,
                       capture_output=True)
        (self.primary / "big.py").write_text(
            "\n".join(f"CHANGED_{i} = {i}" for i in range(6000))
        )
        # Noise of the kind a real working repo carries, which changed_files
        # deliberately reports (anti-hiding) and which used to crowd the prompt.
        (self.primary / ".idea").mkdir(exist_ok=True)
        (self.primary / ".idea" / "workspace.xml").write_text("noise")

        raw = subprocess.run(["git", "-C", str(self.primary), "diff", "HEAD"],
                             capture_output=True, text=True).stdout
        self.assertGreater(len(raw), 131072, "fixture must exceed MAX_ARG_STRLEN")

        p = run_cli(
            self.args("--json", "review", str(self.primary), "review", "REVIEW THIS"),
            env={"AI_OPS_MOCK_BEHAVIOR": "review-promote"},
            timeout=90,
        )
        self.assertNotIn("Argument list too long", p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

        # And the diff really did reach the job, as a file rather than as argv.
        job_id = json.loads(p.stdout)["job_id"]
        events = (self.state / "jobs" / job_id / "evidence" / "events.jsonl").read_bytes()
        self.assertTrue(events, "job produced no evidence")

    def test_atelier_workspace_token_is_not_special_cased(self):
        """Decision, recorded once (atelier ATT-007 asks for it explicitly).

        atelier writes `.atelier-workspace.json` at the worktree root during
        dispatch. agent-ops does NOT exclude that filename from its integrity
        digest, and must not: excluding a name creates a hiding place, which is
        precisely the finding that put untracked and ignored content into the
        digest in the first place.

        No special case is needed, because the per-job delta is before-vs-after
        fingerprints. A token written BEFORE the job has the same fingerprint
        after, so it is never attributed to the job -- while a worker that
        modifies it does show up, which is exactly what atelier refuses at merge.
        """
        token = self.primary / ".atelier-workspace.json"
        token.write_text(json.dumps({"workspaceId": "att-007-token"}))

        p = run_cli(
            self.args("--json", "scout", str(self.primary), "look"),
            env={"AI_OPS_MOCK_BEHAVIOR": "slow-stream", "AI_OPS_MOCK_EXTRA": "0"},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        # Present before the job, untouched by it: not this job's delta.
        job_id = json.loads(p.stdout)["job_id"]
        rec = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        changed = (rec.get("freeze") or {}).get("changed_files") or []
        self.assertNotIn(".atelier-workspace.json", changed)
        # And it is still there -- the rail did not eat the orchestrator's token.
        self.assertTrue(token.exists())

    def test_background_launch_returns_a_job_id_without_waiting(self):
        """The rail used to block for the whole job, so every long run had to be
        hand-backgrounded by its caller."""
        t0 = time.time()
        info = self._launch("6")
        self.assertLess(time.time() - t0, 3.0, "launch blocked on the job")
        self.assertTrue(info["job_id"])
        self.assertEqual(info["state"], "launched")
        try:
            self.assertEqual(self._state_of(info["job_id"]), "running")
            deadline = time.time() + 40
            while time.time() < deadline and self._state_of(info["job_id"]) == "running":
                time.sleep(0.25)
            self.assertEqual(self._state_of(info["job_id"]), "ok")
        finally:
            run_cli(self.args("cancel", info["job_id"]))

    def test_cancel_stops_a_background_job_and_status_says_so(self):
        info = self._launch("30")
        # --json is now required for JSON: `cancel` used to print it regardless
        # of the flag, which is exactly the per-command convention this contract
        # removes.
        p = run_cli(self.args("--json", "cancel", info["job_id"]))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["state"], "cancelled")
        self.assertEqual(self._state_of(info["job_id"]), "cancelled")

    def test_status_and_logs_refuse_an_unknown_job(self):
        """Same family: `status` returned rc=0 with state "unknown" and `logs`
        returned rc=0 with a `progress` event, so a typo'd id -- or an id from a
        state store since wiped -- read as a healthy job that had not started.
        An orchestrator polling it would wait forever on nothing."""
        ghost = "00000000-0000-0000-0000-000000000000"
        for verb in ("status", "logs"):
            p = run_cli(self.args(verb, ghost))
            self.assertNotEqual(p.returncode, 0, f"{verb} accepted a nonexistent job")
            self.assertIn("no such job", p.stderr)

    def test_a_foreground_job_that_dies_is_not_reported_as_running(self):
        """The last hole in the lying-poll family. A backgrounded job has a
        launch record to check liveness against; a FOREGROUND job had none, so
        one whose process died left a job directory with no result.json and
        polled as `running` forever -- measured on a job abandoned five hours
        earlier. Every job now writes pid + starttime + boot_id at start."""
        import signal as _signal

        proc = subprocess.Popen(
            [PYTHON, str(MAIN), *self.args("--json", "scout", str(self.primary), "look")],
            env={**os.environ, "AI_OPS_MOCK_BEHAVIOR": "slow-stream", "AI_OPS_MOCK_EXTRA": "30"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        job_id = None
        deadline = time.time() + 20
        while time.time() < deadline and job_id is None:
            dirs = [d for d in (self.state / "jobs").glob("*") if (d / "runner.json").is_file()]
            if dirs:
                job_id = dirs[0].name
            time.sleep(0.1)
        self.assertIsNotNone(job_id, "no job with a runner record appeared")
        proc.send_signal(_signal.SIGKILL)
        proc.wait(timeout=15)

        deadline = time.time() + 15
        state = None
        while time.time() < deadline:
            p = run_cli(self.args("--json", "status", job_id))
            state = json.loads(p.stdout)["state"]
            if state != "running":
                break
            time.sleep(0.3)
        self.assertEqual(state, "died")

    def test_a_background_job_that_dies_is_not_reported_as_running(self):
        """The worst answer a poll can give is 'running' about a dead process.

        A job killed without writing result.json has no record of its own, so
        status must fall back to the launch record's liveness rather than
        assuming that a missing result means work in progress.
        """
        info = self._launch("30")
        os.kill(info["pid"], signal.SIGKILL)
        deadline = time.time() + 10
        while time.time() < deadline and self._state_of(info["job_id"]) == "running":
            time.sleep(0.1)
        self.assertEqual(self._state_of(info["job_id"]), "died")

    def test_status_answers_while_the_job_is_still_running(self):
        """status is the POLLING answer, so it must work before result.json exists.

        The old status printed the whole persisted record, which (a) only exists
        once the job is over and (b) is the opposite of cheap. A parent agent
        polling in a loop needs state, elapsed, counters -- roughly 30 tokens --
        and should reach for logs only when something looks wrong.
        """
        import threading

        done = []

        def _run():
            run_cli(
                self.args("scout", str(self.primary), "look"),
                env={"AI_OPS_MOCK_BEHAVIOR": "slow-stream", "AI_OPS_MOCK_EXTRA": "6"},
                timeout=90,
            )
            done.append(True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        try:
            job_id = None
            deadline = time.time() + 20
            while time.time() < deadline and not done:
                dirs = [d for d in (self.state / "jobs").glob("*") if d.is_dir()]
                if dirs and (dirs[0] / "evidence" / "events.jsonl").stat().st_size > 0:
                    job_id = dirs[0].name
                    break
                time.sleep(0.05)
            self.assertIsNotNone(job_id, "no job stream appeared")
            p = run_cli(self.args("--json", "status", job_id))
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(out["state"], "running")
            self.assertGreaterEqual(out["elapsed_s"], 0)
            self.assertTrue(out["sessionId"], "no sessionId -- resume would be impossible")
            self.assertLess(len(p.stdout), 600, "status must stay cheap")
        finally:
            t.join(timeout=90)

    def test_logs_digest_is_capped_in_code_and_full_is_never_the_default(self):
        """The bound is enforced, not requested.

        A convention saying "please don't pipe the whole stream into your
        context" gets violated. So digest has a hard byte cap that reports its
        own truncation, and `full` is reachable only by asking for it.
        """
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.cli import DIGEST_MAX_BYTES

        p = run_cli(
            self.args("scout", str(self.primary), "look"),
            env={"AI_OPS_MOCK_BEHAVIOR": "slow-stream", "AI_OPS_MOCK_EXTRA": "0"},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        job_id = [d.name for d in (self.state / "jobs").glob("*") if d.is_dir()][0]

        digest = run_cli(self.args("logs", job_id))
        self.assertEqual(digest.returncode, 0, digest.stderr)
        self.assertLessEqual(len(digest.stdout.encode()), DIGEST_MAX_BYTES + 200)
        self.assertIn('"event":"finished"', digest.stdout)

        full = run_cli(self.args("logs", job_id, "--format", "full"))
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn("step_finish", full.stdout)
        # The raw stream must never be what you get by not choosing.
        self.assertNotEqual(full.stdout, digest.stdout)
        self.assertGreater(len(full.stdout), len(digest.stdout))

    def test_logs_digest_truncates_loudly_rather_than_silently(self):
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.cli import DIGEST_MAX_BYTES

        self.assertGreater(DIGEST_MAX_BYTES, 1024)
        self.assertLess(DIGEST_MAX_BYTES, 65536)

    def test_evidence_is_readable_while_the_job_is_still_running(self):
        """The enabling property for observing a delegated agent mid-run.

        Evidence used to be buffered in a controller tempfile and written only
        after the process exited, so mid-run there was nothing on disk to look
        at. Now stdout streams into evidence/events.jsonl as it arrives.

        The assertion is deliberately TIMED, not just "the file has content while
        the thread has not finished". The mock emits two events, sleeps SLEEP
        seconds, then finishes; so streaming makes content appear almost
        immediately, while write-after-exit cannot produce any until roughly
        SLEEP. Merely checking "content exists and the runner has not returned"
        passes either way, because the post-exit write also lands a beat before
        the runner thread records completion -- that version of this test passed
        against the very behaviour it was supposed to reject.
        """
        import threading

        SLEEP = 6.0
        done = []
        proc_out = []

        def _run():
            proc_out.append(
                run_cli(
                    self.args("scout", str(self.primary), "look"),
                    env={
                        "AI_OPS_MOCK_BEHAVIOR": "slow-stream",
                        "AI_OPS_MOCK_EXTRA": str(SLEEP),
                    },
                    timeout=90,
                )
            )
            done.append(True)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        try:
            started = time.time()
            first_at = None
            blob = b""
            while time.time() - started < SLEEP * 3 and not done:
                hits = list((self.state / "jobs").glob("*/evidence/events.jsonl"))
                if hits and hits[0].stat().st_size > 0:
                    first_at = time.time() - started
                    blob = hits[0].read_bytes()
                    break
                time.sleep(0.05)
            self.assertIsNotNone(
                first_at, "events.jsonl never had content while the job ran"
            )
            self.assertLess(
                first_at,
                SLEEP / 2,
                f"evidence appeared only after {first_at:.2f}s of a {SLEEP}s job -- "
                "that is a post-exit write, not a stream",
            )
            self.assertIn(b"step_start", blob)
            self.assertNotIn(b"step_finish", blob)  # the job is genuinely mid-run
        finally:
            t.join(timeout=90)
        self.assertEqual(proc_out[0].returncode, 0, proc_out[0].stderr)

    def test_a_worker_cannot_seek_back_over_its_own_evidence(self):
        """Streaming must not hand the worker its own record.

        Writing the stream straight into the job's evidence directory is only
        safe because stdout is a PIPE. A regular-file fd would be seekable, and
        a worker could lseek to 0 and truncate away everything it had already
        emitted -- erasing the record of its own run, which is exactly what the
        persist-before-asserts rule exists to prevent.
        """
        p = run_cli(
            self.args("scout", str(self.primary), "look"),
            env={"AI_OPS_MOCK_BEHAVIOR": "rewrite-stdout"},
        )
        hits = list((self.state / "jobs").glob("*/evidence/events.jsonl"))
        self.assertTrue(hits, p.stderr)
        blob = hits[0].read_bytes()
        self.assertIn(b"FIRST-EVENT-MUST-SURVIVE", blob)
        self.assertIn(b"errno=", blob)
        self.assertNotIn(b"seek-succeeded", blob)

    def test_sandbox_env_suppresses_python_bytecode(self):
        """__pycache__ written by a worker would dirty that worker's own freeze.

        The digest covers ignored files on purpose (a worker cannot stage, and
        .gitignore is worker-writable), so bytecode counts as a change. Suppress
        it at the source rather than carving an exception into the digest.
        """
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.env import allowlisted_env

        env = allowlisted_env(home="/tmp/synth")
        self.assertEqual(env.get("PYTHONDONTWRITEBYTECODE"), "1")

    def test_models_listing_reports_reachability(self):
        """The registry catalogues what the rail KNOWS, not what it can REACH.

        openrouter/* ids are listed but no OpenRouter key is installed here. A
        caller choosing a model should learn that from the listing, not from a
        failed job -- otherwise the registry silently over-promises.
        """
        p = run_cli(
            self.args("models"),
            env={"AI_OPS_PROVIDER_CREDENTIAL_FILE": str(self.tmp / "no-such-credential")},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("UNREACHABLE", p.stdout)
        self.assertIn("mode 600", p.stdout)

        cred = self.tmp / "cred"
        cred.write_text("secret\n")
        cred.chmod(0o600)
        p = run_cli(
            self.args("models"),
            env={"AI_OPS_PROVIDER_CREDENTIAL_FILE": str(cred)},
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("reachable", p.stdout)
        self.assertNotIn("UNREACHABLE", p.stdout)

    def test_live_without_a_credential_refuses_rather_than_dropping_the_namespace(self):
        """A job that asks for a live provider but has no credential must refuse.

        --unshare-net is requested only when there is a broker socket to bind, so
        falling through to the no-broker path put the provider process on the
        HOST network. Without a credential it could not reach a model anyway, so
        that fallback bought nothing and silently surrendered the strongest
        containment property the rail has. Fail closed instead.
        """
        p = run_cli(
            self.args("scout", str(self.primary), "look"),
            env={
                "AI_OPS_ALLOW_LIVE_PROVIDER": "1",
                "AI_OPS_PROVIDER_CREDENTIAL_FILE": str(self.tmp / "no-such-credential"),
                "AI_OPS_MOCK_BEHAVIOR": "ok",
            },
        )
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertIn("no credential", p.stderr)

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


    # --- projections must use the JOB's provider, not the caller's profile ---

    def test_a_job_records_which_adapter_ran_it(self):
        """model.provider is the POOL (`opencode-go`), which does not identify
        the code that can read the stream back (`opencode`). Without a separate
        stamp every projection fell through to the ambient profile."""
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"))
        job_id = json.loads(p.stdout)["job_id"]
        rec = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        self.assertEqual(rec["provider"], "opencode")

    def test_the_runner_record_carries_it_too(self):
        """A running job has no result.json, and that is exactly when logs and
        status are used most."""
        info = self._launch()
        runner = self.state / "jobs" / info["job_id"] / "runner.json"
        deadline = time.time() + 15
        while time.time() < deadline and not runner.exists():
            time.sleep(0.2)
        self.assertTrue(runner.exists())
        self.assertEqual(json.loads(runner.read_text())["provider"], "opencode")
        run_cli(self.args("cancel", info["job_id"]))

    def test_logs_work_with_no_profile_at_all(self):
        """The job knows what produced it; the caller should not have to."""
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"))
        job_id = json.loads(p.stdout)["job_id"]
        p2 = run_cli(["--state", str(self.state), "logs", job_id])
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertIn("finished", p2.stdout)

    def test_an_unidentifiable_job_refuses_rather_than_guessing(self):
        """The bug this fixes, in its worst form: reading a Claude job's logs
        under the default profile normalized the stream with the OpenCode
        adapter, recognised nothing, and reported `status: failed, turns: 0,
        'stream truncated ... not evidence of completion'` — for a job that had
        completed fine. A false failure report, produced confidently. Guessing is
        worse than refusing here."""
        job_id = "00000000-0000-4000-8000-0000000000fe"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time()))
        (jd / "evidence" / "events.jsonl").write_text('{"type":"whatever"}\n')
        (jd / "result.json").write_text(json.dumps(
            {"status": "ok", "role": "scout", "mode": "readonly"}))
        p = run_cli(["--state", str(self.state), "logs", job_id])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cannot tell which provider", p.stderr)
        self.assertIn("--profile", p.stderr, "the refusal must name the remedy")

    def test_a_stream_the_adapter_cannot_read_is_called_out(self):
        """A non-empty stream yielding nothing recognisable is the signature of
        the wrong adapter, not of a truncated run."""
        job_id = "00000000-0000-4000-8000-0000000000fd"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time()))
        (jd / "evidence" / "events.jsonl").write_text(
            '{"type":"not_a_shape_this_adapter_knows","x":1}\n' * 5)
        (jd / "result.json").write_text(json.dumps(
            {"status": "ok", "role": "scout", "mode": "readonly",
             "provider": "opencode"}))
        p = run_cli(["--state", str(self.state), "logs", job_id])
        self.assertIn("recognised nothing", p.stderr)
        self.assertIn("different provider", p.stderr)

    # --- the worker is told its limits ---------------------------------------

    def _instructions(self, mode, role):
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.policy import compile_policy
        from ai_ops.profile import load_profile

        return compile_policy(load_profile(str(self.profile)), mode).role_instructions(role)

    def test_the_rail_states_the_sandbox_limits_not_the_spec_author(self):
        """Another project lost two lanes dead-stopped on `.git` being read-only
        before anyone wrote it down, and its GPU-less sandbox produced false
        'wedged GPU' defect reports until every brief was made to say so. The
        launcher injecting the facts is what fixed it there; a spec author cannot
        forget what they never had to write."""
        text = self._instructions("bounded-write", "implement")
        self.assertIn("READ-ONLY", text)
        self.assertIn("index.lock", text, "the actual error the worker will see")
        self.assertIn("No GPU", text)
        self.assertIn("no stdin", text.lower())
        self.assertIn("brokered", text)

    def test_a_limit_is_named_as_the_boundary_not_as_a_defect(self):
        """The expensive failure is not the worker being blocked — it is the
        worker reporting the boundary as a bug in what it is inspecting, or
        burning the job routing around it."""
        text = self._instructions("readonly", "scout")
        self.assertIn("not a defect", text)
        self.assertIn("stop", text.lower())

    def test_the_notice_matches_the_mode(self):
        """A readonly job told 'the worktree is writable' would waste itself
        discovering otherwise."""
        ro = self._instructions("readonly", "scout")
        rw = self._instructions("bounded-write", "implement")
        self.assertIn("worktree is mounted READ-ONLY", ro)
        self.assertNotIn("only writable location", ro)
        self.assertIn("only writable location", rw)

    def test_every_provider_delivers_the_notice(self):
        """OpenCode gets it in a generated agent file, everyone else in the
        prompt. A provider that silently dropped it would run a worker blind."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.adapters import _ADAPTERS

        text = self._instructions("readonly", "scout")
        for name, adapter in _ADAPTERS.items():
            composed = adapter.compose_prompt("DO THE TASK", text)
            if name == "opencode":
                # Delivered out-of-band in the agent file, so the prompt is
                # unchanged — but the notice must genuinely be in that file, or
                # this provider would run its workers blind while the test looked
                # satisfied.
                sys.path.insert(0, str(ROOT / "python"))
                from ai_ops.policy import compile_policy
                from ai_ops.profile import load_profile

                definition = compile_policy(
                    load_profile(str(self.profile)), "readonly"
                ).agent_definition("scout")
                self.assertEqual(composed, "DO THE TASK")
                self.assertIn("No GPU", definition)
                self.assertIn("not a defect", definition)
                continue
            self.assertIn("No GPU", composed, name)
            self.assertIn("DO THE TASK", composed, name)

    # --- process containment ------------------------------------------------

    def test_a_providers_orphaned_children_die_with_the_job(self):
        """Measured on another project running Codex without a pid namespace:
        every job spawned an app-server which spawned MCP servers, upstream
        reaped neither, and it reached 114 app-servers / 754 processes / 15.4GB
        with swap exhausted. Attribution was unsolvable there — the worker is a
        SIBLING of its app-server, not a descendant, so no process-tree walk
        could separate a live lane's servers from a dead one's.

        --unshare-pid makes the question moot: the sandbox is pid 1 of its own
        namespace and the kernel reaps whatever is left in it.

        Detection is a HEARTBEAT, not process matching. The same project fooled
        itself three times with `pgrep -f` — once matching the operator's own
        shell command — and I reproduced that exact failure writing this test
        before switching to a heartbeat. A heartbeat that stops advancing is
        unambiguous; a pattern that matches something is not.
        """
        p = run_cli(self.args("--json", "scout", str(self.primary), "look",
                              "--background"),
                    env={"AI_OPS_MOCK_BEHAVIOR": "spawn-orphan",
                         "AI_OPS_MOCK_HOLD": "6",
                         "AI_OPS_KEEP_SANDBOX_HOME": "1"})
        self.assertEqual(p.returncode, 0, p.stderr)
        job_id = json.loads(p.stdout)["job_id"]
        beat = self.state / "jobs" / job_id / "sandbox-home" / "orphan-heartbeat"

        # 1. The child must actually exist, or the test proves nothing.
        deadline = time.time() + 20
        while time.time() < deadline and not beat.exists():
            time.sleep(0.2)
        self.assertTrue(beat.exists(),
                        "the mock never spawned a child; the test would be vacuous")
        first = beat.read_text()
        deadline = time.time() + 10
        while time.time() < deadline and beat.read_text() == first:
            time.sleep(0.2)
        self.assertNotEqual(beat.read_text(), first,
                            "the child never advanced its heartbeat; not alive")

        # 2. Once the job is over, that heartbeat must stop.
        deadline = time.time() + 60
        while time.time() < deadline and self._state_of(job_id) == "running":
            time.sleep(0.5)
        time.sleep(2.0)  # let the kernel reap, then watch for any further beat
        settled = beat.read_text()
        time.sleep(2.0)
        self.assertEqual(
            beat.read_text(), settled,
            "a sandboxed provider's child outlived its job and is still running "
            "— the pid namespace is not containing it, and this is the 15.4GB "
            "leak class")

    def test_the_sandbox_declares_the_flags_that_make_that_true(self):
        """--unshare-pid without --die-with-parent leaks when the controller
        dies; --die-with-parent without --unshare-pid leaks when the provider
        forks. Both are load-bearing, so both are pinned."""
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops import identity, sandbox
        from ai_ops.policy import compile_policy
        from ai_ops.profile import load_profile

        argv = sandbox.build_bwrap_argv(
            ident=identity.inspect_worktree(str(self.primary)),
            policy=compile_policy(load_profile(str(self.profile)), "readonly"),
            synth_home=str(self.tmp / "home"),
            provider_argv=["/bin/true"],
            command_binds=[],
            broker_socket=None,
            session_binds=[],
        )
        for flag in ("--unshare-pid", "--die-with-parent", "--new-session"):
            self.assertIn(flag, argv, f"{flag} is load-bearing for containment")

    # --- logs --json --------------------------------------------------------

    def test_logs_json_wraps_the_digest_with_its_truncation_state(self):
        """The digest is already JSONL, so --json is about the ENVELOPE: a caller
        gets truncation as a field rather than a sentinel line it must notice."""
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"))
        job_id = json.loads(p.stdout)["job_id"]
        p2 = run_cli(self.args("--json", "logs", job_id))
        self.assertEqual(p2.returncode, 0, p2.stderr)
        out = json.loads(p2.stdout)
        self.assertEqual(out["job_id"], job_id)
        self.assertEqual(out["format"], "digest")
        self.assertIsInstance(out["events"], list)
        self.assertIn("truncated", out)
        self.assertIn("dropped_events", out)

    def test_logs_full_under_json_refuses_rather_than_wrapping(self):
        """`full` is the raw unbounded provider stream. Buffering it into one
        JSON object would defeat the only reason it exists and hand an agent the
        context flood this command is careful to avoid."""
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"))
        job_id = json.loads(p.stdout)["job_id"]
        p2 = run_cli(self.args("--json", "logs", job_id, "--format", "full"))
        self.assertNotEqual(p2.returncode, 0)
        self.assertIn("not available as a JSON object", p2.stderr)
        self.assertIn("events.jsonl", p2.stderr, "must say where to read it instead")

    # --- concurrency cap ----------------------------------------------------

    def _budget(self, **fields):
        path = self.tmp / "budget.json"
        path.write_text(json.dumps(fields))
        return {"AI_OPS_BUDGET_FILE": str(path)}

    def test_absent_config_means_unlimited(self):
        """This is the only step that changes existing behaviour, so an operator
        who has not opted in must see nothing at all."""
        from ai_ops import concurrency

        env = self._budget()  # no max_concurrent_jobs key
        os.environ["AI_OPS_BUDGET_FILE"] = env["AI_OPS_BUDGET_FILE"]
        try:
            self.assertIsNone(concurrency.limit())
            self.assertEqual(concurrency.acquire(str(self.state), "j1", wait=False), 0.0)
            # No marker directory is even created when unlimited.
            self.assertFalse((self.state / "running").exists())
        finally:
            os.environ.pop("AI_OPS_BUDGET_FILE", None)

    def test_a_full_queue_refuses_a_foreground_job_at_once(self):
        """A caller at a terminal wants to be told, not stalled."""
        env = self._budget(max_concurrent_jobs=1)
        p = run_cli(self.args("--json", "scout", str(self.primary), "look",
                              "--background"),
                    env={**env, "AI_OPS_MOCK_BEHAVIOR": "slow-stream",
                         "AI_OPS_MOCK_EXTRA": "8"})
        self.assertEqual(p.returncode, 0, p.stderr)
        first = json.loads(p.stdout)["job_id"]

        deadline = time.time() + 10
        while time.time() < deadline:
            if (self.state / "running").is_dir() and list((self.state / "running").iterdir()):
                break
            time.sleep(0.2)

        started = time.time()
        p2 = run_cli(self.args("scout", str(self.primary), "look"), env=env)
        elapsed = time.time() - started
        self.assertNotEqual(p2.returncode, 0, "second job was allowed past the cap")
        self.assertIn("concurrency limit", p2.stderr)
        self.assertLess(elapsed, 8, "a foreground job waited instead of refusing")
        run_cli(self.args("cancel", first))

    def test_the_refusal_names_every_way_out(self):
        from ai_ops import concurrency
        from ai_ops.errors import Refuse

        os.environ["AI_OPS_BUDGET_FILE"] = self._budget(
            max_concurrent_jobs=1)["AI_OPS_BUDGET_FILE"]
        try:
            concurrency.acquire(str(self.state), "held", wait=False)
            with self.assertRaises(Refuse) as ctx:
                concurrency.acquire(str(self.state), "next", wait=False)
            msg = str(ctx.exception)
            self.assertIn("max_concurrent_jobs", msg)
            self.assertIn("--background", msg)
            self.assertIn("1 of 1", msg)
        finally:
            os.environ.pop("AI_OPS_BUDGET_FILE", None)

    def test_a_crashed_job_does_not_hold_a_slot_forever(self):
        """The failure mode a concurrency cap must not introduce. A stale marker
        is reclaimed by the same liveness check used everywhere else."""
        from ai_ops import concurrency

        os.environ["AI_OPS_BUDGET_FILE"] = self._budget(
            max_concurrent_jobs=1)["AI_OPS_BUDGET_FILE"]
        try:
            rd = self.state / "running"
            rd.mkdir(mode=0o700, exist_ok=True)
            (rd / "ghost.json").write_text(json.dumps(
                {"pid": 2 ** 22, "starttime": "1", "boot_id": "gone", "since": 0}))
            self.assertEqual(concurrency.running(str(self.state)), [],
                             "a dead job's marker still counted")
            self.assertFalse((rd / "ghost.json").exists(),
                             "the stale marker was not reclaimed")
            concurrency.acquire(str(self.state), "new", wait=False)  # must not raise
        finally:
            os.environ.pop("AI_OPS_BUDGET_FILE", None)

    def test_an_unreadable_marker_does_not_hold_a_slot(self):
        from ai_ops import concurrency

        os.environ["AI_OPS_BUDGET_FILE"] = self._budget(
            max_concurrent_jobs=1)["AI_OPS_BUDGET_FILE"]
        try:
            rd = self.state / "running"
            rd.mkdir(mode=0o700, exist_ok=True)
            (rd / "junk.json").write_text("{not json")
            self.assertEqual(concurrency.running(str(self.state)), [])
        finally:
            os.environ.pop("AI_OPS_BUDGET_FILE", None)

    def test_the_slot_is_returned_when_a_job_finishes(self):
        env = self._budget(max_concurrent_jobs=1)
        for _ in range(3):
            p = run_cli(self.args("--json", "scout", str(self.primary), "look"), env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(
            [f for f in os.listdir(self.state / "running")] if (self.state / "running").is_dir() else [],
            [], "a finished job kept its slot")

    def test_queue_time_is_recorded_not_hidden_in_elapsed(self):
        env = self._budget(max_concurrent_jobs=2)
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"), env=env)
        job_id = json.loads(p.stdout)["job_id"]
        rec = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        self.assertIn("queued_s", rec)
        self.assertIsInstance(rec["queued_s"], (int, float))

    # --- gc: what must SURVIVE ----------------------------------------------

    def _aged_job(self, job_id, age_s, status="ok", **extra):
        """A finished job of a given age. Used to test retention without
        waiting an hour for the cool-down floor."""
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time() - age_s))
        rec = {"status": status, "role": "scout", "mode": "readonly"}
        rec.update(extra)
        (jd / "result.json").write_text(json.dumps(rec))
        return jd

    def _gc(self, *args):
        p = run_cli(["--state", str(self.state), "--json", "gc", *args])
        return p, (json.loads(p.stdout) if p.stdout.strip() else {})

    def test_gc_without_a_selector_refuses_and_deletes_nothing(self):
        self._aged_job("00000000-0000-4000-8000-00000000aa01", 90000)
        p = run_cli(["--state", str(self.state), "gc"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("selector", p.stderr)
        self.assertTrue((self.state / "jobs" / "00000000-0000-4000-8000-00000000aa01").exists())

    def test_dry_run_is_the_default(self):
        jd = self._aged_job("00000000-0000-4000-8000-00000000aa02", 90000)
        p, out = self._gc("--older-than", "1h")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(out["dry_run"])
        self.assertIn("00000000-0000-4000-8000-00000000aa02",
                      [c["job_id"] for c in out["jobs"]])
        self.assertTrue(jd.exists(), "dry run must not delete")

    def test_yes_actually_deletes(self):
        jd = self._aged_job("00000000-0000-4000-8000-00000000aa03", 90000)
        p, out = self._gc("--older-than", "1h", "--yes")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("00000000-0000-4000-8000-00000000aa03", out["removed"])
        self.assertFalse(jd.exists())

    def test_awaiting_review_is_never_removed(self):
        """The whole point of the rail: unpromoted work must not be collected."""
        jd = self._aged_job("00000000-0000-4000-8000-00000000aa04", 900000,
                            status="awaiting_review")
        p, out = self._gc("--older-than", "1h", "--yes")
        self.assertTrue(jd.exists(), "awaiting_review job was deleted")
        reasons = {x["job_id"]: x["reason"] for x in out.get("protected", [])}
        self.assertNotIn("00000000-0000-4000-8000-00000000aa04", out.get("removed", []))

    def test_an_unpromoted_review_chain_survives_whole(self):
        """A review whose promotion has not happened is indistinguishable from a
        free-standing one WITHOUT review_of, which is why that key is persisted
        unconditionally. Both halves must survive."""
        subject = "00000000-0000-4000-8000-00000000aa05"
        reviewer = "00000000-0000-4000-8000-00000000aa06"
        sd = self._aged_job(subject, 900000, status="awaiting_review")
        rd = self._aged_job(reviewer, 900000, status="ok", review_of=subject)
        p, out = self._gc("--older-than", "1h", "--yes")
        self.assertTrue(sd.exists(), "subject awaiting review was deleted")
        self.assertTrue(rd.exists(), "its pending review was deleted")

    def test_liveness_unknown_is_protected(self):
        """A missing record is never evidence of a benign state — the rule this
        rail applies everywhere else. Deleting here would be the one place that
        reads absence as death, on the job most likely to be mid-flight."""
        job_id = "00000000-0000-4000-8000-00000000aa07"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time() - 900000))
        # No result.json, no runner.json, no launch record -> state `unknown`.
        p, out = self._gc("--older-than", "1h", "--yes")
        self.assertTrue(jd.exists(), "a job of unknown liveness was deleted")

    def test_the_cooldown_floor_applies_on_top_of_the_selector(self):
        """--older-than 1s must not mean "delete everything"."""
        jd = self._aged_job("00000000-0000-4000-8000-00000000aa08", 60)
        p, out = self._gc("--older-than", "1s", "--yes")
        self.assertTrue(jd.exists(), "cool-down floor did not apply")

    def test_keep_last_keeps_the_most_recent(self):
        ids = []
        for i in range(4):
            jid = f"00000000-0000-4000-8000-00000000ab{i:02d}"
            self._aged_job(jid, 90000 + (4 - i) * 1000)
            ids.append(jid)
        p, out = self._gc("--keep-last", "2", "--yes")
        # ids[0] is oldest by construction; the two newest must survive.
        self.assertTrue((self.state / "jobs" / ids[3]).exists())
        self.assertTrue((self.state / "jobs" / ids[2]).exists())
        self.assertFalse((self.state / "jobs" / ids[0]).exists())

    def test_sessions_need_include_sessions_on_top_of_yes(self):
        """A job directory can be recreated by re-running the job; a session
        store is the only durable copy of a conversation."""
        key = "f" * 64
        sess = self.state / "sessions" / key
        (sess / "opencode").mkdir(parents=True)
        (sess / "worktree.json").write_text(json.dumps(
            {"worktree": str(self.tmp / "deleted-worktree")}))
        p, out = self._gc("--older-than", "1h", "--yes")
        self.assertTrue(sess.exists(), "a bare --yes removed a session store")

        p, out = self._gc("--older-than", "1h", "--yes", "--include-sessions")
        self.assertFalse(sess.exists(), "--include-sessions did not remove it")

    def test_a_session_whose_worktree_still_exists_is_kept(self):
        key = "e" * 64
        sess = self.state / "sessions" / key
        (sess / "opencode").mkdir(parents=True)
        (sess / "worktree.json").write_text(json.dumps({"worktree": str(self.primary)}))
        p, out = self._gc("--older-than", "1h", "--yes", "--include-sessions")
        self.assertTrue(sess.exists())

    def test_an_unidentifiable_session_is_skipped_and_reported(self):
        """Unverifiable is not absent. A store with no marker must be reported,
        never guessed at — preferring to keep something eligible over destroying
        something irreplaceable."""
        key = "d" * 64
        sess = self.state / "sessions" / key
        (sess / "opencode").mkdir(parents=True)
        p, out = self._gc("--older-than", "1h", "--include-sessions")
        self.assertTrue(sess.exists())
        self.assertIn(key, [s["key"] for s in out["sessions_skipped"]])

    def test_orphaned_launch_records_are_swept(self):
        launch = self.state / "launch"
        launch.mkdir(exist_ok=True)
        orphan = launch / "00000000-0000-4000-8000-00000000ac01.json"
        orphan.write_text(json.dumps({"pid": 2 ** 22, "starttime": "1", "boot_id": "x"}))
        p, out = self._gc("--older-than", "1h", "--yes")
        self.assertFalse(orphan.exists())

    def test_protected_entries_explain_themselves(self):
        """A caller who expected a job to go must be able to see which rule kept
        it, rather than concluding gc is broken."""
        self._aged_job("00000000-0000-4000-8000-00000000ac02", 60)
        p, out = self._gc("--older-than", "1h")
        self.assertTrue(out["protected"])
        for entry in out["protected"]:
            self.assertTrue(entry["reason"], "a protected job with no reason")

    def test_reclaimed_bytes_match_du(self):
        """Reported bytes must be what is actually reclaimed. Measured by block
        count and never os.path.getsize, which follows symlinks — Codex symlinks
        ~258MB of binaries into each sandbox home, and that once made a 10MB
        state root report as 2GB."""
        import subprocess as sp

        from ai_ops.gc import _dir_bytes

        jd = self._aged_job("00000000-0000-4000-8000-00000000ac03", 90000)
        (jd / "evidence" / "events.jsonl").write_text("x" * 50000)
        du = int(sp.run(["du", "-s", "--block-size=1", str(jd)],
                        capture_output=True, text=True).stdout.split()[0])
        self.assertEqual(_dir_bytes(str(jd)), du)

    def test_a_symlink_is_not_counted_as_its_target(self):
        """The measurement bug that nearly shaped the retention design."""
        from ai_ops.gc import _dir_bytes

        jd = self._aged_job("00000000-0000-4000-8000-00000000ac04", 90000)
        big = self.tmp / "big-binary"
        big.write_bytes(b"\0" * 2_000_000)
        os.symlink(big, jd / "linked")
        self.assertLess(_dir_bytes(str(jd)), 500_000,
                        "symlink target was counted as reclaimable")

    # --- secret scanning of worker OUTPUT ----------------------------------

    def test_a_leaking_job_is_flagged_but_still_completes(self):
        """Flag, never destroy. The run already happened and already cost money;
        failing it would lose the work AND the evidence of the leak."""
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"),
                    env={"AI_OPS_MOCK_BEHAVIOR": "leak-secret"})
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = json.loads(p.stdout)
        job_id = rec["job_id"]
        full = json.loads((self.state / "jobs" / job_id / "result.json").read_text())

        self.assertEqual(full["status"], "ok", "a leak must not change the status")
        found = full.get("secrets_suspected")
        self.assertTrue(found, "leaked key was not flagged")
        self.assertEqual(found[0]["pattern"], "xai-key")
        self.assertIn("evidence/events.jsonl", found[0]["where"])
        self.assertIn("WARNING", p.stderr)

    def test_the_leaked_value_is_not_copied_into_the_record(self):
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"),
                    env={"AI_OPS_MOCK_BEHAVIOR": "leak-secret"})
        job_id = json.loads(p.stdout)["job_id"]
        raw = (self.state / "jobs" / job_id / "result.json").read_text()
        self.assertNotIn("k" * 20, raw,
                         "result.json must not become a second copy of the secret")
        self.assertNotIn("k" * 20, p.stderr, "the warning must not print the value")

    def test_evidence_is_left_byte_intact(self):
        """The rail records honestly and points at the problem; it never edits
        what it recorded."""
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"),
                    env={"AI_OPS_MOCK_BEHAVIOR": "leak-secret"})
        job_id = json.loads(p.stdout)["job_id"]
        ev = (self.state / "jobs" / job_id / "evidence" / "events.jsonl").read_text()
        self.assertIn("xai-" + "k" * 40, ev,
                      "evidence was altered; it must stay byte-intact")

    def test_a_clean_job_carries_no_finding(self):
        p = run_cli(self.args("--json", "scout", str(self.primary), "look"),
                    env={"AI_OPS_MOCK_BEHAVIOR": "ok"})
        job_id = json.loads(p.stdout)["job_id"]
        full = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        self.assertNotIn("secrets_suspected", full)

    # --- effort ------------------------------------------------------------

    def test_effort_from_the_profile_lands_on_the_record(self):
        """Profile-owned, end to end. The mock is opencode-shaped and its model
        has no measured effort set, so this also proves the refusal is real
        rather than a lint on a string."""
        prof = json.loads(self.profile.read_text())
        prof["roles"]["scout"]["effort"] = "high"
        self.profile.write_text(json.dumps(prof, indent=2))
        p = run_cli(self.args("scout", str(self.primary), "hello"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("deepseek-v4-flash", p.stderr, "must name the MODEL, not the provider")
        self.assertIn("per-MODEL", p.stderr, "must say why it cannot be inferred")
        self.assertIn("registry.json", p.stderr, "must say where to record it")

    def test_a_role_without_effort_records_null(self):
        p = run_cli(self.args("--json", "scout", str(self.primary), "hello"))
        self.assertEqual(p.returncode, 0, p.stderr)
        job_id = json.loads(p.stdout)["job_id"]
        rec = json.loads((self.state / "jobs" / job_id / "result.json").read_text())
        self.assertIsNone(rec["effort"], "absent effort must be recorded, not omitted")

    def test_an_unusable_effort_costs_nothing(self):
        """Refused before the job directory exists, so a bad profile cannot
        litter the state root or touch the budget."""
        prof = json.loads(self.profile.read_text())
        prof["roles"]["scout"]["effort"] = "high"
        self.profile.write_text(json.dumps(prof, indent=2))
        before = sorted(os.listdir(self.state / "jobs"))
        run_cli(self.args("scout", str(self.primary), "hello"))
        self.assertEqual(sorted(os.listdir(self.state / "jobs")), before)

    # --- `jobs` listing -------------------------------------------------
    # A state root's contents were entirely unlistable before this command.
    def _run_scout_job(self):
        p = run_cli(self.args("--json", "scout", str(self.primary), "hello"))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["job_id"]

    def test_lists_a_completed_job(self):
        job_id = self._run_scout_job()
        p = run_cli(["--state", str(self.state), "--json", "jobs"])
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        row = next(r for r in out["jobs"] if r["job_id"] == job_id)
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["role"], "scout")
        self.assertFalse(row["awaiting_review"])
        self.assertIsNotNone(row["elapsed_s"])

    def test_crashed_job_reads_died_not_running(self):
        """The bug this command exists for: a job whose process is gone but
        which never wrote a result was previously indistinguishable from a
        healthy running job."""
        job_id = "00000000-0000-4000-8000-00000000dead"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time() - 300))
        # A pid that is certainly not alive, with the same triple a real runner
        # writes, so liveness is decided by the record and not by its absence.
        (jd / "runner.json").write_text(json.dumps(
            {"pid": 2 ** 22, "starttime": "1", "boot_id": "nope"}))
        p = run_cli(["--state", str(self.state), "--json", "jobs"])
        out = json.loads(p.stdout)
        row = next(r for r in out["jobs"] if r["job_id"] == job_id)
        self.assertEqual(row["state"], "died")

    def test_dead_job_elapsed_is_frozen_not_wall_clock(self):
        """A job dead for an hour must not report an hour of runtime."""
        job_id = "00000000-0000-4000-8000-0000000001d0"
        jd = self.state / "jobs" / job_id
        (jd / "evidence").mkdir(parents=True)
        started = time.time() - 3600
        (jd / "started_at").write_text(str(started))
        (jd / "runner.json").write_text(json.dumps(
            {"pid": 2 ** 22, "starttime": "1", "boot_id": "nope"}))
        os.utime(jd / "runner.json", (started + 5, started + 5))
        p = run_cli(["--state", str(self.state), "--json", "jobs"])
        row = next(r for r in json.loads(p.stdout)["jobs"] if r["job_id"] == job_id)
        self.assertLess(row["elapsed_s"], 60, "dead job measured against now")

    def test_filters_and_limit(self):
        job_id = self._run_scout_job()
        p = run_cli(["--state", str(self.state), "--json", "jobs",
                     "--state-filter", "died"])
        self.assertEqual(json.loads(p.stdout)["jobs"], [])

        p = run_cli(["--state", str(self.state), "--json", "jobs",
                     "--worktree", str(self.primary)])
        self.assertIn(job_id, [r["job_id"] for r in json.loads(p.stdout)["jobs"]])

        p = run_cli(["--state", str(self.state), "--json", "jobs",
                     "--worktree", str(self.sibling)])
        self.assertEqual(json.loads(p.stdout)["jobs"], [])

        # An aged job is excluded by a window that does not reach it, and
        # included by one that does.
        old_id = "00000000-0000-4000-8000-0000000ac1d0"
        jd = self.state / "jobs" / old_id
        jd.mkdir(parents=True)
        (jd / "started_at").write_text(str(time.time() - 7200))
        (jd / "result.json").write_text(json.dumps({"status": "ok", "role": "scout"}))

        ids = lambda window: [
            r["job_id"] for r in json.loads(
                run_cli(["--state", str(self.state), "--json", "jobs",
                         "--since", window]).stdout)["jobs"]
        ]
        self.assertNotIn(old_id, ids("1h"))
        self.assertIn(old_id, ids("24h"))
        self.assertIn(job_id, ids("24h"))

    def test_truncation_is_declared(self):
        for _ in range(3):
            self._run_scout_job()
        p = run_cli(["--state", str(self.state), "--json", "jobs", "--limit", "1"])
        out = json.loads(p.stdout)
        self.assertEqual(len(out["jobs"]), 1)
        self.assertTrue(out["truncated"])
        self.assertGreaterEqual(out["total"], 3)
        p = run_cli(["--state", str(self.state), "--json", "jobs", "--all"])
        self.assertFalse(json.loads(p.stdout)["truncated"])

    def test_empty_listing_is_not_an_error(self):
        p = run_cli(["--state", str(self.state), "--json", "jobs"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["jobs"], [])

    def test_bad_duration_is_refused_by_name(self):
        p = run_cli(["--state", str(self.state), "jobs", "--since", "yesterday"])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("yesterday", p.stderr)
        self.assertIn("30m", p.stderr, "refusal must name the accepted form")


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


class RealProviderVocabularyUnit(unittest.TestCase):
    """The committed mock invented an event vocabulary the real provider does
    not use. These tests pin the SHAPE a live OpenCode run actually produces,
    so the suite stops validating a fiction."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    # Captured from a real `opencode run --format json` (1.18.18).
    REAL = (
        b'{"type":"step_start","part":{"type":"step-start"}}\n'
        b'{"type":"tool_use","part":{"type":"tool","tool":"read"}}\n'
        b'{"type":"text","part":{"type":"text","text":"1. divide has no zero check."}}\n'
        b'{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}\n'
    )

    def test_real_stream_is_accepted(self):
        from ai_ops.events import parse_event_stream

        term = parse_event_stream(self.REAL, require_handoff=False)
        self.assertEqual(term["type"], "step_finish")
        self.assertIn("divide has no zero check", term["_text"])

    def test_real_error_event_is_a_provider_error(self):
        from ai_ops.errors import ProviderError
        from ai_ops.events import parse_event_stream

        raw = b'{"type":"error","error":{"name":"APIError","data":{"message":"Forbidden"}}}\n'
        with self.assertRaises(ProviderError) as ctx:
            parse_event_stream(raw, require_handoff=False)
        self.assertIn("Forbidden", str(ctx.exception))

    @staticmethod
    def _stream(model_text: str) -> bytes:
        """Build a realistic stream: model text, then a step_finish."""
        lines = [
            json.dumps({"type": "text",
                        "part": {"type": "text", "text": model_text}}),
            json.dumps({"type": "step_finish",
                        "part": {"type": "step-finish", "reason": "stop"}}),
        ]
        return ("\n".join(lines) + "\n").encode()

    def test_handoff_is_read_from_model_text_on_a_live_run(self):
        """Real OpenCode never emits a `handoff` object -- the model writes it
        as text, so the rail must parse it out of the assistant output."""
        from ai_ops.events import parse_event_stream

        body = json.dumps({"handoff": {"summary": "fixed divide",
                                       "status": "awaiting_review"}})
        raw = self._stream("Done.\n```json\n" + body + "\n```")
        term = parse_event_stream(raw, require_handoff=True)
        self.assertEqual(term["_handoff"]["summary"], "fixed divide")

    def test_review_verdict_is_read_from_model_text(self):
        from ai_ops.events import extract_review_verdict

        body = json.dumps({"review": {"verdict": "promote",
                                      "reviewed_files": ["calc.py"],
                                      "findings": []}})
        verdict, findings, files = extract_review_verdict(
            self._stream("```json\n" + body + "\n```"))
        self.assertEqual(verdict, "promote")
        self.assertEqual(files, ["calc.py"])

    def test_prose_without_a_structured_object_is_refused(self):
        """A model that just talks must not be read as an approval."""
        from ai_ops.errors import ProviderError
        from ai_ops.events import extract_review_verdict

        with self.assertRaises(ProviderError):
            extract_review_verdict(self._stream("Looks good to me, ship it."))


class FindingsGateUnit(unittest.TestCase):
    """A promote verdict must not override the reviewer's own serious findings."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def test_blocking_severities_detected(self):
        from ai_ops.review import blocking_findings

        f = [{"severity": "low", "claim": "nit"},
             {"severity": "high", "claim": "real bug"},
             {"severity": "info", "claim": "fyi"}]
        self.assertEqual(len(blocking_findings(f)), 1)
        self.assertEqual(blocking_findings([{"severity": "LOW"}]), [])
        self.assertEqual(len(blocking_findings([{"severity": "Critical"}])), 1)

    def test_promote_refuses_on_high_severity_findings(self):
        import tempfile

        from ai_ops.errors import Refuse
        from ai_ops.review import promote
        from ai_ops.state import atomic_write_json

        d = Path(tempfile.mkdtemp(prefix="aiops-findings-"))
        subj = d / "result.json"
        freeze = {"head": "H", "tree_digest": "T", "policy_digest": "P",
                  "models_registry_digest": "R", "changed_files": ["a.py"]}
        atomic_write_json(str(subj), {
            "job_id": "S", "status": "awaiting_review", "mode": "bounded-write",
            "role": "implement", "model": {"id": "m", "provider": "p"}, "dir": str(d),
            "exit": 0, "started": "s", "generation": 0, "freeze": freeze,
        })
        art = {"subject_job": "S", "reviewer_job": "R",
               "model": {"id": "m2", "provider": "p"},
               "role": "review", "independence": {"different_job": True,
               "different_model": True, "different_family": True,
               "different_provider": False}, "verdict": "promote",
               "subject_head": "H", "subject_tree_digest": "T",
               "subject_policy_digest": "P", "reviewed_dir": str(d),
               "reviewed_tree_digest": "T", "models_registry_digest": "R",
               "reviewed_files": ["a.py"], "required_unmet": [],
               "findings": [{"severity": "high", "claim": "negative qty unvalidated"}]}
        with self.assertRaises(Refuse) as ctx:
            promote(subject_path=str(subj), review_artifact=art, live_head="H",
                    live_tree_digest="T", expected_files=["a.py"], generation=0)
        self.assertIn("disqualifying finding", str(ctx.exception))
        # and it still promotes when the findings are only advisory
        art["findings"] = [{"severity": "low", "claim": "style"}]
        rec = promote(subject_path=str(subj), review_artifact=art, live_head="H",
                      live_tree_digest="T", expected_files=["a.py"], generation=0)
        self.assertEqual(rec["status"], "ok")


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
            # /messages is a legitimate inference surface (Anthropic-shaped
            # models route there); denying it broke qwen3.8-max in a live probe.
            self.assertNotEqual(post("/v1/messages", "opencode-go/glm-5.3"), 403)
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
