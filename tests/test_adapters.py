#!/usr/bin/env python3
"""Adapter / normalized-vocabulary tests, anchored on a REAL captured stream.

tests/fixtures/opencode-real-scout.jsonl was captured from a live scout run
against opencode-go/deepseek-v4-flash on 2026-08-18. It exists because the
single worst defect in this project's history was a committed mock that invented
an event vocabulary the provider never emits: 64 tests validated a fiction, and
the entire promote chain could not have run against a live model. A real stream
in the repo is the antidote -- the fixture is the authority and the mock is the
thing under suspicion.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
FIXTURE = ROOT / "tests" / "fixtures" / "opencode-real-scout.jsonl"

from ai_ops.adapters import (  # noqa: E402
    TERMINAL_COMPLETED,
    TERMINAL_EMPTY,
    TERMINAL_FAILED,
    get_adapter,
    parse_lenient,
)
from ai_ops.errors import Refuse  # noqa: E402


class RealStream(unittest.TestCase):
    def setUp(self):
        self.raw = FIXTURE.read_text()
        self.events, self.used = parse_lenient(self.raw)
        self.adapter = get_adapter("opencode")

    def test_the_fixture_still_carries_the_real_vocabulary(self):
        """Tripwire. If someone 'fixes' this fixture to match the mock, fail.

        Real OpenCode emits step_start / tool_use / text / step_finish and
        carries sessionID on every event. It does NOT emit `complete` -- that
        type is the mock's invention, and asserting its absence here is what
        stops the fiction being re-adopted as truth.
        """
        types = {e.get("type") for e in self.events}
        self.assertEqual(types, {"step_start", "tool_use", "text", "step_finish"})
        self.assertNotIn("complete", types)
        for ev in self.events:
            self.assertTrue(ev.get("sessionID"), f"no sessionID on {ev.get('type')}")

    def test_normalize_extracts_the_load_bearing_fields(self):
        norm = self.adapter.normalize(self.events)
        status = [n for n in norm if n["event"] == "status"]
        self.assertEqual(len(status), 1)
        # Without a session id there is no resume, so this is the one field
        # whose absence would make the whole reply-by-restart design impossible.
        self.assertTrue(status[0]["sessionId"].startswith("ses_"))

        fin = [n for n in norm if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_COMPLETED)
        self.assertEqual(fin["turns"], 3)
        self.assertGreater(fin["costUSD"], 0)
        self.assertGreater(fin["tokens"], 0)
        self.assertTrue(fin["exitSummary"])

        tools = [n for n in norm if n["event"] == "tool"]
        self.assertEqual([t["name"] for t in tools], ["glob", "read"])
        self.assertTrue(all(t["target"] for t in tools))

    def test_every_prefix_of_a_real_stream_parses_without_raising(self):
        """Projections read the file WHILE it is being written.

        The last object is routinely half-flushed, so a strict parser would make
        `status` and `logs --follow` fail at random mid-run. Every prefix must
        yield whatever is complete and drop the partial tail.
        """
        for cut in range(0, len(self.raw), 37):
            evs, used = parse_lenient(self.raw[:cut])
            self.assertLessEqual(used, cut)
            self.adapter.normalize(evs)  # must not raise at any cut

    def test_session_id_is_recoverable_from_a_partial_stream(self):
        """Resume must survive a job that is still running or died mid-flight."""
        half, _ = parse_lenient(self.raw[: len(self.raw) // 2])
        self.assertTrue(half)
        self.assertTrue(self.adapter.session_id(half).startswith("ses_"))

    def test_normalized_form_is_drastically_smaller_than_the_raw_stream(self):
        """The whole point: a projection a parent agent can afford to read."""
        norm = self.adapter.normalize(self.events)
        size = len(json.dumps(norm))
        self.assertLess(size * 4, len(self.raw))


class Vocabulary(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("opencode")

    def test_a_run_with_no_assistant_text_is_completed_empty(self):
        """Not a success to report as one -- atelier distinguishes the two."""
        evs = [
            {"type": "step_start", "sessionID": "ses_x", "part": {}},
            {"type": "step_finish", "sessionID": "ses_x", "part": {"reason": "stop"}},
        ]
        fin = [n for n in self.adapter.normalize(evs) if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_EMPTY)

    def test_text_is_bounded_at_write_time(self):
        evs = [{"type": "text", "sessionID": "ses_x", "part": {"text": "A" * 5000}}]
        norm = self.adapter.normalize(evs)
        text = [n for n in norm if n["event"] == "text"][0]
        self.assertLess(len(text["content"]), 500)

    def test_a_truncated_stream_never_reports_completed(self):
        """"Claims done, evidence truncated" is the suspicious case.

        A run that ended without the provider closing its stream has produced no
        evidence of completion, however much assistant text it emitted first.
        Reporting `completed` there is what atelier's tripwires exist to catch,
        and over-reporting truncation is the right default.
        """
        evs = [
            {"type": "step_start", "sessionID": "ses_x", "part": {}},
            {"type": "text", "sessionID": "ses_x", "part": {"text": "All done, tests pass!"}},
            # no step_finish: the stream was cut off
        ]
        fin = [
            n for n in self.adapter.normalize(evs, run_ended=True) if n["event"] == "finished"
        ][0]
        self.assertEqual(fin["status"], TERMINAL_FAILED)
        self.assertFalse(fin["sawTerminal"])
        self.assertIn("truncated", fin["exitSummary"])
        self.assertNotIn("All done", fin["exitSummary"])

    def test_a_running_job_emits_no_terminal_event_at_all(self):
        """An adapter tails this stream for `finished` to transition the record.

        The same partial stream means "still working" during a job and
        "truncated" after one, so the normalizer takes run_ended from the caller
        rather than guessing. While the job runs it emits counters as `progress`
        -- emitting `finished` would transition the record early, every time.
        """
        evs = [
            {"type": "step_start", "sessionID": "ses_x", "part": {}},
            {"type": "text", "sessionID": "ses_x", "part": {"text": "working"}},
        ]
        norm = self.adapter.normalize(evs, run_ended=False)
        kinds = {n["event"] for n in norm}
        self.assertNotIn("finished", kinds)
        self.assertIn("progress", kinds)
        prog = [n for n in norm if n["event"] == "progress"][0]
        self.assertEqual(prog["turns"], 1)

    def test_a_terminal_stream_is_finished_even_if_the_caller_says_otherwise(self):
        """run_ended only ADDS knowledge; a real terminal event still finishes."""
        evs = [
            {"type": "step_start", "sessionID": "ses_x", "part": {}},
            {"type": "text", "sessionID": "ses_x", "part": {"text": "done"}},
            {"type": "step_finish", "sessionID": "ses_x", "part": {"reason": "stop"}},
        ]
        norm = self.adapter.normalize(evs, run_ended=False)
        fin = [n for n in norm if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_COMPLETED)

    def test_unknown_provider_refuses_rather_than_guessing(self):
        with self.assertRaises(Refuse):
            get_adapter("grok")

    def test_adapter_keys_off_the_binary_not_the_model_pool(self):
        """`opencode-go` and `openrouter` are model POOLS reached through the
        same binary. Selecting an adapter by pool would break the moment a
        second pool appears behind one CLI, which is already the case."""
        with self.assertRaises(Refuse):
            get_adapter("opencode-go")


class ExecutionPinning(unittest.TestCase):
    """Content pin, not path pin (atelier ATT-006, owner ruling)."""

    def test_digest_covers_the_package_not_just_the_launcher(self):
        """The launcher is an 11-line stub that execs the package.

        A resolved-path pin freezes the one file that never changes; a digest of
        only the launcher file does the same thing one step later. Editing any
        package source must move the digest.
        """
        import tempfile

        from ai_ops import pinning

        pkg = Path(tempfile.mkdtemp(prefix="pin-")) / "ai_ops"
        pkg.mkdir()
        (pkg / "a.py").write_text("x = 1\n")
        launcher = pkg.parent / "launch.sh"
        launcher.write_text("#!/bin/sh\n")

        first = pinning.launcher_digest(str(launcher), str(pkg))
        (pkg / "a.py").write_text("x = 2\n")
        self.assertNotEqual(first, pinning.launcher_digest(str(launcher), str(pkg)))

        (pkg / "a.py").write_text("x = 1\n")
        self.assertEqual(first, pinning.launcher_digest(str(launcher), str(pkg)))

        # A rename with identical bytes must also move it, which digesting
        # concatenated content would miss.
        (pkg / "a.py").rename(pkg / "b.py")
        self.assertNotEqual(first, pinning.launcher_digest(str(launcher), str(pkg)))

    def test_bytecode_is_excluded_so_the_digest_does_not_depend_on_imports(self):
        import tempfile

        from ai_ops import pinning

        pkg = Path(tempfile.mkdtemp(prefix="pin2-")) / "ai_ops"
        (pkg / "__pycache__").mkdir(parents=True)
        (pkg / "a.py").write_text("x = 1\n")
        before = pinning.launcher_digest("/nonexistent-launcher", str(pkg))
        (pkg / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00compiled")
        self.assertEqual(before, pinning.launcher_digest("/nonexistent-launcher", str(pkg)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
