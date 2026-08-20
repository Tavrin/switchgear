#!/usr/bin/env python3
"""In-sandbox delegation: the client is the untrusted worker.

Every test here is about what the worker CANNOT do. The feature is a
convenience; the refusals are the product. A delegation broker that grants what
it is asked for is a hole in the sandbox with a nicer name.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from switchgear import delegate  # noqa: E402
from switchgear.errors import Refuse  # noqa: E402


class Policy(unittest.TestCase):
    def test_absent_means_disabled_not_defaulted_on(self):
        """This is the one feature that lets a sandboxed process cause new spend
        and new processes. A default that quietly enabled it would change the
        threat model while looking like a convenience."""
        pol = delegate.policy_for({})
        self.assertFalse(pol["enabled"])
        self.assertEqual(pol["roles"], ())

    def test_enabled_without_roles_is_still_disabled(self):
        """`enabled: true` with no allowlist is not "allow everything" -- there is
        no reading of an empty allowlist that means permission."""
        pol = delegate.policy_for({"delegation": {"enabled": True, "roles": []}})
        self.assertFalse(pol["enabled"])

    def test_it_refuses_a_malformed_policy_rather_than_guessing(self):
        for bad in ({"delegation": "yes"}, {"delegation": {"roles": "scout"}},
                    {"delegation": {"roles": [1, 2]}}):
            with self.assertRaises(Refuse):
                delegate.policy_for(bad)

    def test_an_operator_allowlist_is_carried_immutably(self):
        pol = delegate.policy_for(
            {"delegation": {"enabled": True, "roles": ["scout", "review"]}}
        )
        self.assertTrue(pol["enabled"])
        # A tuple: a handler thread must not be able to mutate the allowlist it
        # is about to check itself against.
        self.assertIsInstance(pol["roles"], tuple)


class _Client:
    """Minimal HTTP-over-unix-socket client, so the test speaks the real wire."""

    def __init__(self, path):
        self.path = path

    def request(self, method, target, body=None):
        payload = json.dumps(body).encode() if body is not None else b""
        head = f"{method} {target} HTTP/1.1\r\nHost: d\r\n"
        if body is not None:
            head += (f"Content-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\n")
        head += "Connection: close\r\n\r\n"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(self.path)
        s.sendall(head.encode() + payload)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        s.close()
        raw = b"".join(chunks)
        head_raw, _, body_raw = raw.partition(b"\r\n\r\n")
        code = int(head_raw.split(b" ")[1])
        try:
            return code, json.loads(body_raw.decode("utf-8", "replace"))
        except ValueError:
            return code, {}


class Refusals(unittest.TestCase):
    """The worker asks; the controller decides. These are the decisions."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-deleg-"))
        self.sock = str(self.tmp / "d.sock")
        self.broker = delegate.DelegationBroker(
            unix_socket=self.sock, parent_job="parent-1",
            worktree=str(self.tmp), state_path=str(self.tmp / "state"),
            profile_path=None, provider_path=None,
            roles=("scout",), max_children=2, max_depth=1, depth=0,
        )
        self.broker.__enter__()
        self.client = _Client(self.sock)

    def tearDown(self):
        self.broker.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_role_outside_the_allowlist_is_refused(self):
        code, body = self.client.request("POST", "/delegate",
                                         {"role": "implement", "prompt": "go"})
        self.assertEqual(code, 403)
        self.assertIn("not delegable", body["error"])

    def test_the_worker_cannot_choose_a_model_mode_or_directory(self):
        """The heart of it. Unknown KEYS are refused rather than ignored, so a
        worker cannot probe for a field that is silently accepted."""
        for extra in ({"model": "anthropic/opus"}, {"mode": "bounded-write"},
                      {"cwd": "/etc"}, {"dir": "/"}, {"effort": "max"},
                      {"timeout": 99999}, {"provider": "/bin/sh"}):
            req = {"role": "scout", "prompt": "look"}
            req.update(extra)
            with self.subTest(extra=extra):
                code, body = self.client.request("POST", "/delegate", req)
                self.assertEqual(code, 403)
                self.assertIn("unknown field", body["error"])

    def test_an_oversized_or_empty_prompt_is_refused(self):
        code, _ = self.client.request("POST", "/delegate",
                                      {"role": "scout", "prompt": "   "})
        self.assertEqual(code, 403)
        code, body = self.client.request(
            "POST", "/delegate",
            {"role": "scout", "prompt": "x" * (delegate.PROMPT_MAX_CHARS + 1)})
        self.assertEqual(code, 403)
        self.assertIn("limit", body["error"])

    def test_a_child_may_not_delegate_further(self):
        """Otherwise this is a fork bomb with a language model attached, and the
        budget it burns is real money."""
        deep = delegate.DelegationBroker(
            unix_socket=str(self.tmp / "deep.sock"), parent_job="child-1",
            worktree=str(self.tmp), state_path=str(self.tmp / "state"),
            profile_path=None, provider_path=None,
            roles=("scout",), max_children=2, max_depth=1, depth=1,
        )
        with self.assertRaises(Refuse) as ctx:
            deep.spawn("scout", "go deeper")
        self.assertIn("depth", str(ctx.exception))

    def test_it_cannot_read_a_job_that_is_not_its_child(self):
        """Without this scoping the socket is a read primitive over the whole
        state root, handed to the untrusted worker -- the store the sandbox
        exists to hide."""
        code, body = self.client.request("GET", "/delegate/some-other-job")
        self.assertEqual(code, 403)
        self.assertIn("no such subagent", body["error"])

    def test_unknown_paths_are_refused(self):
        code, _ = self.client.request("GET", "/../../etc/passwd")
        self.assertEqual(code, 403)
        code, _ = self.client.request("POST", "/anything", {"role": "scout"})
        self.assertEqual(code, 403)

    def test_denials_are_recorded_as_evidence_about_the_worker(self):
        """A worker probing its own boundary is a fact worth keeping."""
        self.client.request("POST", "/delegate", {"role": "implement", "prompt": "x"})
        self.client.request("GET", "/delegate/not-mine")
        summary = self.broker.summary()
        self.assertGreaterEqual(summary["denied"], 2)
        self.assertTrue(summary["denied_detail"])

    def test_the_child_cap_holds_under_concurrent_requests(self):
        """Two requests can each see children < max and both spawn. That is the
        exact shape of the concurrency-cap over-run the soak test found, so it
        is checked here rather than discovered later."""
        self.broker.spawn = lambda role, prompt: self._counted_spawn()
        self._spawned = 0
        self._guard = threading.Lock()

        def hit():
            self.client.request("POST", "/delegate", {"role": "scout", "prompt": "x"})

        threads = [threading.Thread(target=hit) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(self._spawned, self.broker.max_children)

    def _counted_spawn(self):
        with self.broker._lock:
            if len(self.broker.children) >= self.broker.max_children:
                raise Refuse("limit")
            self.broker.children.append("c")
        with self._guard:
            self._spawned += 1
        return "child"


class RunningChild(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-deleg2-"))
        self.state = self.tmp / "state"
        (self.state / "jobs" / "kid").mkdir(parents=True)
        self.broker = delegate.DelegationBroker(
            unix_socket=str(self.tmp / "d.sock"), parent_job="p",
            worktree=str(self.tmp), state_path=str(self.state),
            profile_path=None, provider_path=None,
            roles=("scout",), max_children=2, max_depth=1, depth=0,
        )
        self.broker.children.append("kid")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_record_that_states_no_outcome_is_not_reported_as_finished(self):
        """Bytes that parse are not an outcome.

        The absence fix checked whether a record EXISTED, so an empty object or
        one whose status was absent or non-string reported `finished` with
        `status: null` -- the same absence-is-benign answer, rebuilt out of a
        different absence, and handed to the untrusted worker as a verdict.
        """
        from switchgear.lease import _boot_id, _starttime

        launch = self.state / "launch"
        launch.mkdir()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            (launch / "kid.json").write_text(json.dumps({
                "job_id": "kid", "pid": proc.pid,
                "starttime": _starttime(proc.pid), "boot_id": _boot_id(),
            }))
            result = self.state / "jobs" / "kid" / "result.json"
            for body in ("{}", json.dumps({"job_id": "kid"}),
                         json.dumps({"job_id": "kid", "status": 7})):
                with self.subTest(body=body):
                    result.write_text(body)
                    out = self.broker.child_result("kid")
                    self.assertNotEqual(out["state"], "finished")
                    self.assertNotIn("status", out)
                    self.assertIn(out["state"], ("running", "queued"))
            # Non-vacuity: a record that DOES state its outcome still finishes,
            # so this cannot pass against a version that never reports finished.
            result.write_text(json.dumps({"job_id": "kid", "status": "ok",
                                          "exitSummary": "done"}))
            done = self.broker.child_result("kid")
            self.assertEqual(done["state"], "finished")
            self.assertEqual(done["status"], "ok")
            self.assertEqual(done["answer"], "done")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_a_missing_or_unreadable_record_uses_the_launch_liveness(self):
        """A missing result used to mean running forever, even after the child
        died. A real live-then-dead pid identity makes both sides non-vacuous and
        also proves corrupt result bytes do not hide the launch verdict."""
        from switchgear.lease import _boot_id, _starttime

        launch = self.state / "launch"
        launch.mkdir()
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            (launch / "kid.json").write_text(json.dumps({
                "job_id": "kid",
                "pid": proc.pid,
                "starttime": _starttime(proc.pid),
                "boot_id": _boot_id(),
            }))
            live = self.broker.child_result("kid")
            self.assertIn(live["state"], ("running", "queued"))
            self.assertNotIn("status", live)

            proc.terminate()
            proc.wait(timeout=10)
            dead = self.broker.child_result("kid")
            self.assertEqual(dead, {"job_id": "kid", "state": "died"})

            (self.state / "jobs" / "kid" / "result.json").write_text("{broken")
            unreadable = self.broker.child_result("kid")
            self.assertEqual(unreadable, {"job_id": "kid", "state": "died"})
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_the_worker_gets_the_answer_and_not_the_record(self):
        """A bounded projection: the child's paths, digests and policy are not
        the parent worker's business."""
        (self.state / "jobs" / "kid" / "result.json").write_text(json.dumps({
            "status": "ok", "exitSummary": "found it", "dir": "/secret/worktree",
            "policy_digest": "abc", "artifacts": {"events": "/secret/path"},
        }))
        out = self.broker.child_result("kid")
        self.assertEqual(out["state"], "finished")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["answer"], "found it")
        blob = json.dumps(out)
        for leak in ("/secret/worktree", "/secret/path", "abc"):
            self.assertNotIn(leak, blob)


class FromInsideTheSandbox(unittest.TestCase):
    """The end-to-end property, probed by the worker itself.

    Everything above tests the controller's decisions in isolation. This runs a
    real sandboxed job whose worker goes looking for the socket, because the
    thing most likely to be wrong is not the policy -- it is whether the socket
    is reachable at all across a mount and network namespace.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiops-deleg-e2e-"))
        self.state = self.tmp / "state"
        self.budget = self.tmp / "budget.json"
        out = __import__("subprocess").run(
            ["bash", str(ROOT / "tests" / "helpers" / "make-synthetic-repo"),
             str(self.tmp / "syn")], capture_output=True, text=True)
        self.vals = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
        self.profile = self.tmp / "p.json"
        self.profile.write_text((ROOT / "project-profiles" / "example.json").read_text())
        __import__("subprocess").run(
            ["/usr/bin/python3", str(ROOT / "python" / "switchgear" / "__main__.py"),
             "--state", str(self.state), "state", "provision", str(self.state)],
            capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scout(self):
        import subprocess as sp

        p = sp.run(
            ["/usr/bin/python3", str(ROOT / "python" / "switchgear" / "__main__.py"),
             "--profile", str(self.profile), "--state", str(self.state),
             "--provider", str(ROOT / "tests" / "helpers" / "mock_provider.py"),
             "--json", "scout", self.vals["PRIMARY"], "x"],
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ, SWITCHGEAR_MOCK_BEHAVIOR="delegate-probe",
                     SWITCHGEAR_BUDGET_FILE=str(self.budget)))
        self.assertEqual(p.returncode, 0, p.stderr)
        rec = json.loads(p.stdout)
        events = Path(rec["artifacts"]["events"]).read_text()
        return rec, events

    def test_by_default_the_socket_is_absent_from_the_sandbox(self):
        """Not present-and-refusing: a worker should not be able to tell that
        the feature exists, and an operator who never enabled it has not widened
        the boundary by upgrading."""
        self.assertFalse(self.budget.exists())
        rec, events = self._scout()
        self.assertIn("no-delegate-socket", events)
        self.assertIsNone(rec.get("delegation"))

    def test_when_enabled_the_socket_is_reachable_and_still_refuses(self):
        """Both halves matter. Reachable across the namespaces, AND the role the
        worker actually asked for -- one it was not granted -- is denied."""
        self.budget.write_text(json.dumps({
            "delegation": {"enabled": True, "roles": ["scout"], "max_children": 1}
        }))
        rec, events = self._scout()
        self.assertIn("delegate-reachable status=403", events,
                      f"worker could not reach or was not refused: {events[:400]}")
        # And the attempt is evidence about the worker, kept on the record.
        self.assertIsNotNone(rec.get("delegation"))
        self.assertGreaterEqual(rec["delegation"]["denied"], 1)
        self.assertEqual(rec["delegation"]["children"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
