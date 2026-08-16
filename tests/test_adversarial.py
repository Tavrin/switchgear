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
        self._acquire()
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
        a = subprocess.Popen(
            [PYTHON, str(MAIN), *self.args("write", str(self.wt), "implement", "--envelope", str(envf))],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.4)
        b = subprocess.run(
            [PYTHON, str(MAIN), *self.args("write", str(self.wt), "implement", "--envelope", str(envf))],
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


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "python"))
    unittest.main(verbosity=2)
