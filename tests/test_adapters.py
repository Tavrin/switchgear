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

    def test_unknown_provider_refuses_rather_than_guessing(self):
        with self.assertRaises(Refuse):
            get_adapter("grok")

    def test_adapter_keys_off_the_binary_not_the_model_pool(self):
        """`opencode-go` and `openrouter` are model POOLS reached through the
        same binary. Selecting an adapter by pool would break the moment a
        second pool appears behind one CLI, which is already the case."""
        with self.assertRaises(Refuse):
            get_adapter("opencode-go")


if __name__ == "__main__":
    unittest.main(verbosity=2)
