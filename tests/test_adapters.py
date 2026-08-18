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
            get_adapter("gemini")

    def test_adapter_keys_off_the_binary_not_the_model_pool(self):
        """`opencode-go` and `openrouter` are model POOLS reached through the
        same binary. Selecting an adapter by pool would break the moment a
        second pool appears behind one CLI, which is already the case."""
        with self.assertRaises(Refuse):
            get_adapter("opencode-go")


GROK_FIXTURE = ROOT / "tests" / "fixtures" / "grok-real-scout.jsonl"


class GrokRealStream(unittest.TestCase):
    """Anchored on tests/fixtures/grok-real-scout.jsonl, captured live
    2026-08-18 from grok 1.0.4 / grok-4.6-build. Same rule as the OpenCode
    fixture: the capture is the authority, documentation is not."""

    def setUp(self):
        self.raw = GROK_FIXTURE.read_text()
        self.events, self.used = parse_lenient(self.raw)
        self.adapter = get_adapter("grok")

    def test_the_fixture_still_carries_the_real_vocabulary(self):
        """Tripwire against the fixture being 'corrected' toward a fiction.

        The real dialect: text/thought as DELTAS, sessionId ONLY in `end`,
        cost pre-totalled. None of that matches what the design docs would have
        predicted, which is the whole reason the fixture exists.
        """
        types = {e.get("type") for e in self.events}
        self.assertEqual(
            types,
            {"available_commands", "thought", "text", "usage",
             "tool_call", "tool_call_update", "end"},
        )
        with_sid = [e["type"] for e in self.events if "sessionId" in e]
        self.assertEqual(with_sid, ["end"], "sessionId must live only in `end`")
        # Deltas, not whole messages: many tiny text events, not one big one.
        text_evs = [e for e in self.events if e["type"] == "text"]
        self.assertGreater(len(text_evs), 5)
        self.assertLess(max(len(e.get("data") or "") for e in text_evs), 40)

    def test_normalize_coalesces_deltas_and_reads_the_end_totals(self):
        norm = self.adapter.normalize(self.events, run_ended=True)
        texts = [n for n in norm if n["event"] == "text"]
        self.assertEqual(len(texts), 1, "deltas must coalesce into ONE text event")
        # The CONTRACT is that the coalesced text is exactly the concatenated
        # deltas, clipped. Asserting a phrase the model happened to say made this
        # test fail on a re-capture with a different prompt — punishing exactly
        # the fixture refresh the freshness check asks for.
        joined = "".join(str(e.get("data") or "") for e in self.events
                         if e["type"] == "text")
        self.assertTrue(texts[0]["content"])
        self.assertTrue(joined.startswith(texts[0]["content"][:40]))

        # Totals come from the provider's own `end` event, never recomputed.
        end_ev = [e for e in self.events if e["type"] == "end"][0]
        fin = [n for n in norm if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_COMPLETED)
        self.assertEqual(fin["turns"], int(end_ev["num_turns"]))
        self.assertGreater(fin["turns"], 0)
        self.assertGreater(fin["costUSD"], 0)
        self.assertGreater(fin["tokens"], 0)
        self.assertTrue(fin["sawTerminal"])

        sid = [n for n in norm if n["event"] == "status"][0]["sessionId"]
        self.assertEqual(sid, self.adapter.session_id(self.events))

        tools = [n for n in norm if n["event"] == "tool"]
        self.assertIn("read_file", [t["name"] for t in tools])

    def test_thought_deltas_never_reach_the_digest(self):
        """Reasoning belongs to the full stream for a human, not to a projection
        that lands in a parent agent's context."""
        norm = self.adapter.normalize(self.events, run_ended=True)
        blob = json.dumps(norm)
        for ev in self.events:
            if ev["type"] == "thought" and len(ev.get("data") or "") > 8:
                self.assertNotIn(ev["data"], blob)

    def test_truncated_grok_stream_never_reports_completed(self):
        """Cut the stream before `end`: text exists, evidence of completion
        does not. Same honesty rule as OpenCode, via the shared policy."""
        cut = [e for e in self.events if e["type"] != "end"]
        fin = [
            n for n in self.adapter.normalize(cut, run_ended=True)
            if n["event"] == "finished"
        ][0]
        self.assertEqual(fin["status"], TERMINAL_FAILED)
        self.assertIn("truncated", fin["exitSummary"])

    def test_a_running_grok_job_emits_progress_not_finished(self):
        cut = [e for e in self.events if e["type"] != "end"]
        kinds = {n["event"] for n in self.adapter.normalize(cut, run_ended=False)}
        self.assertNotIn("finished", kinds)
        self.assertIn("progress", kinds)

    def test_an_abnormal_stop_reason_is_failed_not_guessed_benign(self):
        """Only end_turn was observed as a normal close. Anything else must be
        reported with the provider's own word, not assumed fine."""
        evs = [dict(e) for e in self.events]
        for e in evs:
            if e["type"] == "end":
                e["stopReason"] = "refusal"
        fin = [
            n for n in self.adapter.normalize(evs, run_ended=True)
            if n["event"] == "finished"
        ][0]
        self.assertEqual(fin["status"], TERMINAL_FAILED)
        self.assertIn("refusal", fin["exitSummary"])

    def test_every_prefix_parses_without_raising(self):
        for cut in range(0, len(self.raw), 211):
            evs, _ = parse_lenient(self.raw[:cut])
            self.adapter.normalize(evs)


CLAUDE_FIXTURE = ROOT / "tests" / "fixtures" / "claude-real-scout.jsonl"


class ClaudeRealStream(unittest.TestCase):
    """Anchored on a stream captured 2026-08-18 from Claude Code 2.1.235 with a
    CLEAN HOME and the OAuth token in ANTHROPIC_AUTH_TOKEN -- i.e. a recording of
    the exact full-tier configuration this adapter runs."""

    def setUp(self):
        self.raw = CLAUDE_FIXTURE.read_text()
        self.events, _ = parse_lenient(self.raw)
        self.adapter = get_adapter("claude")

    def test_the_fixture_still_carries_the_real_vocabulary(self):
        types = {(e.get("type"), e.get("subtype")) for e in self.events}
        self.assertIn(("system", "init"), types)
        self.assertIn(("result", "success"), types)
        self.assertIn(("assistant", None), types)
        # session_id is snake_case and on EVERY event -- unlike Grok's sessionId
        # which appears only in its terminal event.
        self.assertTrue(all("session_id" in e for e in self.events))
        # Content arrives as whole blocks, not deltas.
        blocks = [b for e in self.events if e.get("type") == "assistant"
                  for b in e["message"].get("content", [])]
        self.assertTrue(any(b.get("type") == "text" for b in blocks))
        self.assertTrue(any(b.get("type") == "thinking" for b in blocks))

    def test_normalize_reads_blocks_and_result_totals(self):
        norm = self.adapter.normalize(self.events, run_ended=True)
        self.assertTrue(
            [n for n in norm if n["event"] == "status"][0]["sessionId"]
        )
        fin = [n for n in norm if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_COMPLETED)
        # Read OFF the stream, not hardcoded. How many turns a given run took is
        # a property of that recording, not of the vocabulary — asserting the
        # literal number made this test fail on a re-capture that was otherwise
        # byte-for-byte compatible, which punishes exactly the fixture refresh
        # the freshness check asks for. What must hold is that the adapter
        # reports the provider's OWN count rather than inventing one.
        result_ev = [e for e in self.events if e.get("type") == "result"][0]
        self.assertEqual(fin["turns"], int(result_ev["num_turns"]))
        self.assertGreater(fin["turns"], 0)
        self.assertAlmostEqual(fin["costUSD"], result_ev["total_cost_usd"], places=6)
        self.assertGreater(fin["costUSD"], 0)
        self.assertGreater(fin["tokens"], 0)
        tools = [n["name"] for n in norm if n["event"] == "tool"]
        self.assertIn("Read", tools)

    def test_thinking_blocks_never_reach_the_digest(self):
        norm = self.adapter.normalize(self.events, run_ended=True)
        blob = json.dumps(norm)
        for e in self.events:
            if e.get("type") != "assistant":
                continue
            for b in e["message"].get("content", []):
                if b.get("type") == "thinking" and len(b.get("thinking") or "") > 8:
                    self.assertNotIn(b["thinking"], blob)

    def test_is_error_result_is_reported_failed(self):
        """`is_error` is an explicit flag; it must not be inferred or ignored."""
        evs = [dict(e) for e in self.events]
        for e in evs:
            if e.get("type") == "result":
                e["is_error"] = True
                e["result"] = "the model hit an execution error"
        fin = [n for n in self.adapter.normalize(evs, run_ended=True)
               if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_FAILED)
        self.assertIn("execution error", fin["exitSummary"])

    def test_truncated_claude_stream_never_reports_completed(self):
        cut = [e for e in self.events if e.get("type") != "result"]
        fin = [n for n in self.adapter.normalize(cut, run_ended=True)
               if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_FAILED)

    def test_claude_is_full_tier_and_uses_the_oauth_header(self):
        """ANTHROPIC_AUTH_TOKEN -> Authorization: Bearer. ANTHROPIC_API_KEY would
        select the BYOK x-api-key path instead (measured)."""
        self.assertFalse(getattr(self.adapter, "credential_in_sandbox", False))
        env = self.adapter.isolation_env("/tmp/h", {}, "http://127.0.0.1:8099")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "broker-placeholder-not-a-credential")
        self.assertIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)


CODEX_FIXTURE = ROOT / "tests" / "fixtures" / "codex-real-scout.jsonl"


class CodexRealStream(unittest.TestCase):
    """Anchored on a stream captured 2026-08-18 from codex-cli 0.147.0."""

    def setUp(self):
        self.raw = CODEX_FIXTURE.read_text()
        self.events, _ = parse_lenient(self.raw)
        self.adapter = get_adapter("codex")

    def test_the_fixture_still_carries_the_real_vocabulary(self):
        types = {e.get("type") for e in self.events}
        self.assertEqual(
            types,
            {"thread.started", "turn.started", "item.started", "item.completed",
             "turn.completed"},
        )
        # thread_id is on the FIRST event, unlike Grok's sessionId which is only
        # on its terminal event -- resume info exists from job start.
        self.assertEqual(self.events[0]["type"], "thread.started")
        self.assertTrue(self.events[0].get("thread_id"))

    def test_normalize_reads_items_and_usage(self):
        norm = self.adapter.normalize(self.events, run_ended=True)
        self.assertTrue([n for n in norm if n["event"] == "status"][0]["sessionId"])
        fin = [n for n in norm if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_COMPLETED)
        self.assertGreater(fin["tokens"], 0)
        # Codex reports NO cost. Report 0.0 rather than invent one.
        self.assertEqual(fin["costUSD"], 0.0)
        tools = [n["name"] for n in norm if n["event"] == "tool"]
        self.assertIn("command_execution", tools)

    def test_an_error_item_is_a_failure_even_when_the_turn_closes_normally(self):
        """Measured: a run whose code-mode host was missing emitted an error
        item, answered "I can't inspect the file", and closed its turn normally
        -- so it read as completed. Claims-done-with-evidence-of-failure."""
        evs = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {"type": "item.completed",
             "item": {"id": "i0", "type": "error", "message": "code-mode host missing"}},
            {"type": "item.completed",
             "item": {"id": "i1", "type": "agent_message", "text": "I can't inspect it."}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
        ]
        fin = [n for n in self.adapter.normalize(evs, run_ended=True)
               if n["event"] == "finished"][0]
        self.assertEqual(fin["status"], TERMINAL_FAILED)
        self.assertIn("code-mode host", fin["exitSummary"])

    def test_codex_is_full_tier_and_forces_the_http_transport(self):
        """supports_websockets=false is load-bearing: by default Codex reaches
        inference over wss://api.openai.com/v1/responses, which ignores the
        base-url redirect and which an HTTP broker cannot proxy at all."""
        import tempfile as _tf

        self.assertFalse(getattr(self.adapter, "credential_in_sandbox", False))
        home = _tf.mkdtemp(prefix="cdxenv-")
        env = self.adapter.isolation_env(home, {}, "http://127.0.0.1:8099")
        self.assertTrue(env["CODEX_HOME"].endswith(".codex"))
        cfg = Path(env["CODEX_HOME"], "config.toml").read_text()
        self.assertIn("supports_websockets = false", cfg)
        self.assertIn("http://127.0.0.1:8099", cfg)
        # A placeholder session is written; the real token stays controller-side.
        auth = json.loads(Path(env["CODEX_HOME"], "auth.json").read_text())
        self.assertIsNone(auth["OPENAI_API_KEY"])
        self.assertEqual(auth["tokens"]["account_id"], self.adapter.PLACEHOLDER_ACCOUNT)

    def test_extra_binds_include_the_helper_binaries(self):
        """Binding only the executable gave a job that authenticated and answered
        while reporting "the workspace execution tool is unavailable"."""
        import os as _os

        from ai_ops.compat import PINNED_PROVIDERS

        real = (PINNED_PROVIDERS.get("codex") or {}).get("path")
        if not real or not _os.path.exists(real):
            self.skipTest("no discovered codex install on this machine")
        binds = self.adapter.extra_binds([real])
        self.assertTrue(binds)
        self.assertTrue(_os.path.isdir(_os.path.join(binds[0], "bin")))


class WriteLane(unittest.TestCase):
    """The bounded-write contract across providers.

    OpenCode receives the role instructions in a generated agent file; every
    other provider has no such mechanism and gets the SAME text in its prompt.
    One source, two delivery paths -- two copies would drift, and a worker told a
    different contract from the one the rail validates fails in a way that looks
    like a model problem.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))
        from ai_ops.policy import compile_policy
        from ai_ops.profile import load_profile

        prof = json.loads((ROOT / "project-profiles" / "example.json").read_text())
        prof["write_enabled"] = True
        self.pol = compile_policy(prof, "bounded-write")
        self.instructions = self.pol.role_instructions("implement")

    def test_the_agent_file_and_the_prompt_carry_the_same_contract(self):
        agent_file = self.pol.agent_definition("implement")
        # The handoff contract appears in the OpenCode agent file...
        self.assertIn('"handoff"', agent_file)
        # ...and in the text every other provider puts in its prompt.
        self.assertIn('"handoff"', self.instructions)
        for line in self.instructions.strip().splitlines():
            self.assertIn(line, agent_file)

    def test_opencode_leaves_the_prompt_alone(self):
        a = get_adapter("opencode")
        self.assertEqual(a.compose_prompt("do the thing", self.instructions), "do the thing")

    def test_other_providers_get_the_instructions_in_the_prompt(self):
        for name in ("claude", "codex", "grok"):
            composed = get_adapter(name).compose_prompt("do the thing", self.instructions)
            self.assertIn("do the thing", composed)
            self.assertIn('"handoff"', composed, f"{name} lost the contract")

    def _raw(self, name, text):
        """A minimal terminal stream for each provider carrying `text`."""
        if name == "claude":
            evs = [{"type": "system", "subtype": "init", "session_id": "s"},
                   {"type": "assistant", "session_id": "s",
                    "message": {"content": [{"type": "text", "text": text}]}},
                   {"type": "result", "subtype": "success", "session_id": "s",
                    "is_error": False, "num_turns": 1, "total_cost_usd": 0.01,
                    "usage": {"input_tokens": 1, "output_tokens": 1}}]
        elif name == "codex":
            evs = [{"type": "thread.started", "thread_id": "t"},
                   {"type": "turn.started"},
                   {"type": "item.completed",
                    "item": {"id": "i", "type": "agent_message", "text": text}},
                   {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
        else:
            evs = [{"type": "text", "sessionID": "s", "data": text},
                   {"type": "end", "stopReason": "end_turn", "sessionId": "s",
                    "num_turns": 1, "total_cost_usd": 0.001, "usage": {"total_tokens": 5}}]
        return "\n".join(json.dumps(e) for e in evs).encode()

    def test_a_valid_handoff_is_extracted_on_every_provider(self):
        handoff = {"handoff": {"summary": "added a zero check", "status": "awaiting_review",
                               "changes": ["calc.py"], "next_action": "review"}}
        text = "Done.\n\n```json\n" + json.dumps(handoff) + "\n```"
        for name in ("claude", "codex", "grok"):
            obj, err = get_adapter(name).validate_result(
                self._raw(name, text), require_handoff=True)
            self.assertIsNone(err, f"{name}: {err}")
            self.assertEqual(obj["status"], "awaiting_review")

    def test_a_missing_handoff_is_rejected_on_every_provider(self):
        for name in ("claude", "codex", "grok"):
            obj, err = get_adapter(name).validate_result(
                self._raw(name, "I finished, trust me."), require_handoff=True)
            self.assertIsNone(obj)
            self.assertIn("handoff", err or "", f"{name} accepted a write with no handoff")

    def test_a_schema_invalid_handoff_is_rejected(self):
        """A worker's structured claim must satisfy the same schema whichever
        provider produced it -- status is an enum, not free text."""
        bad = {"handoff": {"summary": "x", "status": "totally-done"}}
        text = "```json\n" + json.dumps(bad) + "\n```"
        for name in ("claude", "codex", "grok"):
            obj, err = get_adapter(name).validate_result(
                self._raw(name, text), require_handoff=True)
            self.assertIsNone(obj)
            self.assertTrue(err)

    def test_handoff_survives_a_long_answer_that_the_digest_clips(self):
        """Normalized text events are clipped to 400 chars for the digest. The
        handoff must come from the UNCLIPPED text or long-but-valid work is
        rejected with 'missing handoff'."""
        handoff = {"handoff": {"summary": "s", "status": "awaiting_review",
                               "changes": ["a.py"] * 30}}
        text = "Report. " + ("x" * 400) + "\n\n```json\n" + json.dumps(handoff) + "\n```"
        for name in ("claude", "codex", "grok"):
            obj, err = get_adapter(name).validate_result(
                self._raw(name, text), require_handoff=True)
            self.assertIsNone(err, f"{name} lost the handoff to clipping: {err}")
            self.assertEqual(obj["status"], "awaiting_review")

    def test_write_argv_asks_for_write_permission_per_provider(self):
        """Each CLI gates edits differently; headless has no one to answer a
        prompt. The OS boundary is the control, so these flags are unblocking a
        redundant in-process gate, not widening the boundary."""
        common = dict(provider_argv=["/x"], worktree="/w", model_id="p/m",
                      role="implement", job_id="j", prompt="do")
        claude = get_adapter("claude").argv(agent="ai-ops-bounded-write", **common)
        self.assertIn("--permission-mode", claude)
        codex = get_adapter("codex").argv(agent="ai-ops-bounded-write", **common)
        self.assertIn("workspace-write", codex)
        grok = get_adapter("grok").argv(agent="ai-ops-bounded-write", **common)
        self.assertIn("--always-approve", grok)
        # And a READONLY job must not carry any of them.
        ro = get_adapter("codex").argv(agent="ai-ops-readonly", **common)
        self.assertIn("read-only", ro)
        self.assertNotIn("workspace-write", ro)
        self.assertNotIn("--always-approve",
                         get_adapter("grok").argv(agent="ai-ops-readonly", **common))


class ModelIdentityDerivation(unittest.TestCase):
    """Identity is DERIVED from controller-owned rules, not hand-listed.

    Model versions churn weekly -- `opencode models` reported 26 ids where the
    registry had hand-listed 18 across every provider -- so a curated list is
    stale the day after it is written. What must NOT move is who owns the
    metadata: reviewer independence rests on family/vendor, so the rules live in
    the controller and a project profile can still only NAME ids.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def test_new_model_versions_classify_without_a_code_change(self):
        from ai_ops.registry import model_record

        for mid, family, vendor in [
            ("opencode-go/kimi-k2.7-code", "kimi", "moonshot"),
            ("opencode-go/qwen3.7-max", "qwen", "alibaba"),
            ("opencode-go/minimax-m3", "minimax", "minimax"),
            # Deliberately FICTIONAL ids: the case that matters is a model
            # released after this code was written, which cannot be tested with
            # a present-day name. Named so nobody mistakes the suite for a
            # catalogue of models you can actually call.
            ("grok/grok-does-not-exist-9", "grok", "xai"),
            ("codex/gpt-vnext-fictional", "gpt", "openai"),
            ("claude/claude-haiku-4-5", "claude", "anthropic"),
        ]:
            rec = model_record(mid)
            self.assertEqual(rec["model_family"], family, mid)
            self.assertEqual(rec["vendor_family"], vendor, mid)
            self.assertEqual(rec["identity_source"], "derived", mid)

    def test_a_curated_entry_still_wins(self):
        """Derivation is a default, not an override: a curated entry is how a
        family the rules get wrong gets fixed."""
        from ai_ops.registry import model_record

        rec = model_record("opencode-go/deepseek-v4-flash")
        self.assertEqual(rec["identity_source"], "registry")

    def test_the_single_vendor_provider_settles_the_vendor(self):
        """A model served by the Grok CLI is xAI's whatever it is called."""
        from ai_ops.registry import model_record

        self.assertEqual(model_record("grok/some-unreleased-thing")["vendor_family"], "xai")

    def test_a_vendor_segment_in_the_id_is_honoured(self):
        from ai_ops.registry import model_record

        rec = model_record("openrouter/anthropic/claude-sonnet-9")
        self.assertEqual(rec["vendor_family"], "anthropic")

    def test_an_unknown_provider_still_refuses(self):
        """Derivation loosened MODELS, not providers: an unknown provider has no
        upstream and no credential, so it cannot be reached at all."""
        from ai_ops.errors import Refuse
        from ai_ops.registry import model_record

        with self.assertRaises(Refuse):
            model_record("not-a-provider/some-model")

    def test_the_deny_list_still_applies_to_derived_models(self):
        """A profile must not reach a denied model just because nobody curated
        an entry for it."""
        from ai_ops.errors import Refuse
        from ai_ops.registry import model_record

        with self.assertRaises(Refuse):
            model_record("openai/gpt-4")


class ResumeContract(unittest.TestCase):
    """Steering is turn-based: every provider resumes natively, keyed on the
    session id the rail already captures."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def _argv(self, name, **over):
        base = dict(provider_argv=["/BIN"], worktree="/w", model_id="p/m",
                    agent="ai-ops-readonly", role="scout", job_id="j", prompt="MSG")
        base.update(over)
        return get_adapter(name).argv(**base)

    def test_every_provider_builds_its_own_resume_form(self):
        """The forms genuinely differ: codex resumes via a SUBCOMMAND, the rest
        via a flag, and opencode's flag is --session rather than --resume."""
        oc = self._argv("opencode", resume_session="SID")
        self.assertIn("--session", oc)
        self.assertIn("SID", oc)

        cl = self._argv("claude", resume_session="SID")
        self.assertIn("--resume", cl)

        gk = self._argv("grok", resume_session="SID")
        self.assertIn("--resume", gk)

        cx = self._argv("codex", resume_session="SID")
        self.assertEqual(cx[1:3], ["exec", "resume"])
        self.assertEqual(cx[3], "SID")

    def test_no_resume_session_leaves_argv_untouched(self):
        for name in ("opencode", "claude", "grok", "codex"):
            argv = self._argv(name)
            self.assertNotIn("--resume", argv, name)
            self.assertNotIn("--session", argv, name)
            self.assertNotIn("resume", argv[1:3], name)

    def test_session_stores_are_measured_per_provider(self):
        """Each path was observed in a real sandbox HOME, not guessed. An empty
        list means resume is REFUSED for that provider, because guessing would
        produce a fresh conversation wearing the previous session's id -- a
        continuation in name only."""
        self.assertIn(".claude/projects", get_adapter("claude").session_store_paths())
        self.assertIn(".codex/sessions", get_adapter("codex").session_store_paths())
        self.assertIn(".grok/sessions", get_adapter("grok").session_store_paths())
        self.assertIn(".local/share/opencode", get_adapter("opencode").session_store_paths())

    def test_grok_persists_only_sessions_not_its_whole_config_dir(self):
        """~/.grok holds auth.json beside sessions/. Persisting the parent would
        persist the credential -- the fallback tier writes a real access token
        there."""
        paths = get_adapter("grok").session_store_paths()
        self.assertNotIn(".grok", paths)
        self.assertTrue(all(p.startswith(".grok/") for p in paths))

    def test_a_credential_in_a_session_store_is_refused(self):
        """Some stores sit beside a credential on the host (OpenCode keeps
        auth.json in the same data dir as its session db). Nothing can write one
        there today -- which is an observation about the current configuration,
        not a property of it."""
        import tempfile

        from ai_ops.errors import Refuse
        from ai_ops.job import _assert_no_credentials

        store = Path(tempfile.mkdtemp(prefix="store-"))
        (store / "sessions").mkdir()
        (store / "sessions" / "history.jsonl").write_text("{}\n")
        _assert_no_credentials(str(store))  # clean store: fine

        (store / "sessions" / "auth.json").write_text("{}")
        with self.assertRaises(Refuse) as cm:
            _assert_no_credentials(str(store))
        self.assertIn("looks like a credential", str(cm.exception))

    def test_session_stores_never_include_the_credential_directory(self):
        """Persisting conversation state must not persist credentials: only the
        named subpaths are bound, never the whole provider config directory."""
        for name in ("claude", "codex", "grok", "opencode"):
            for path in get_adapter(name).session_store_paths():
                self.assertNotIn("auth", path.lower(), f"{name}: {path}")
                self.assertNotIn("credential", path.lower(), f"{name}: {path}")
                self.assertNotEqual(path.rstrip("/"), ".claude", name)
                self.assertNotEqual(path.rstrip("/"), ".codex", name)


class AddingAProvider(unittest.TestCase):
    """The claim under test: a new harness (a Mistral subscription, say) is a
    small, well-defined amount of work with loud failures — not archaeology
    across four existing adapters."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def _minimal(self):
        """A complete adapter, written to the interface and nothing else. If
        this stops being short, the seam has regressed."""
        from ai_ops.adapters import ProviderAdapter

        class MistralAdapter(ProviderAdapter):
            name = "mistral"

            def required_flags(self):
                return ["run", "--model", "--json"]

            def argv(self, *, provider_argv, worktree, model_id, agent, role,
                     job_id, prompt, attach_dir=None, resume_session=None,
                     effort=None):
                wire = model_id.split("/", 1)[-1]
                return list(provider_argv) + ["run", "--json", "--model", wire, prompt]

            def normalize(self, events, run_ended=False):
                return [{"event": "text", "content": str(e.get("text", ""))}
                        for e in events]

            def validate_result(self, raw, *, require_handoff):
                return None, None

            def session_id(self, events):
                return next((e.get("sid") for e in events if e.get("sid")), None)

            def isolation_env(self, synth_home, runtime, broker_base_url=None):
                return {"HOME": synth_home}

            def broker_runtime(self, runtime, base_url, model_id):
                return dict(runtime, base_url=base_url)

        return MistralAdapter()

    def test_a_new_adapter_needs_only_the_required_methods(self):
        """Seven methods, no boilerplate: the optional surface is inherited."""
        from ai_ops.adapters import EFFORT_UNSUPPORTED, validate_adapter

        adapter = self._minimal()
        validate_adapter("mistral", adapter)  # must not raise

        # Everything optional has an honest default, without a line written.
        self.assertEqual(adapter.session_store_paths(), [])
        self.assertIsNone(adapter.refresh_argv(["/bin/x"]))
        self.assertIsNone(adapter.list_models_argv(["/bin/x"]))
        self.assertEqual(adapter.extra_binds(["/bin/x"]), [])
        self.assertEqual(adapter.effort_support()["status"], EFFORT_UNSUPPORTED)
        self.assertFalse(adapter.credential_in_sandbox)
        self.assertEqual(adapter.version_argv(["/bin/x"]), ["/bin/x", "--version"])
        self.assertEqual(adapter.agent_name("bounded-write"), "ai-ops-bounded-write")

    def test_defaults_are_the_honest_negative_not_a_guess(self):
        """A provider that cannot resume must REFUSE to resume, not quietly
        start a fresh conversation dressed as a continuation."""
        adapter = self._minimal()
        self.assertEqual(adapter.session_store_paths(), [],
                         "an unmeasured provider must not claim resume support")

    def test_an_incomplete_adapter_is_refused_at_registration(self):
        """Previously it registered fine and died of AttributeError partway
        through a job — after the sandbox was built and, live, after spending."""
        from ai_ops.adapters import ProviderAdapter, validate_adapter
        from ai_ops.errors import Refuse

        class HalfWritten(ProviderAdapter):
            name = "half"

            def argv(self, **kw):
                return []

        with self.assertRaises(Refuse) as ctx:
            validate_adapter("half", HalfWritten())
        msg = str(ctx.exception)
        self.assertIn("normalize", msg, "the refusal must name what is missing")
        self.assertIn("ADDING-A-PROVIDER", msg, "and where to look")

    def test_a_non_conforming_object_is_refused(self):
        from ai_ops.adapters import validate_adapter
        from ai_ops.errors import Refuse

        class NotAnAdapter:
            name = "nope"

        with self.assertRaises(Refuse):
            validate_adapter("nope", NotAnAdapter())

    def test_a_name_mismatch_is_refused(self):
        """get_adapter keys off the registry key and the profile names the same
        string; a disagreement would pick the wrong adapter silently."""
        from ai_ops.adapters import validate_adapter
        from ai_ops.errors import Refuse

        with self.assertRaises(Refuse) as ctx:
            validate_adapter("mistral-large", self._minimal())
        self.assertIn("mistral", str(ctx.exception))

    def test_every_shipped_adapter_passes_its_own_check(self):
        from ai_ops.adapters import _ADAPTERS, validate_adapter

        for name, adapter in _ADAPTERS.items():
            validate_adapter(name, adapter)

    def test_the_new_adapter_actually_builds_a_command_line(self):
        argv = self._minimal().argv(
            provider_argv=["/usr/bin/mistral"], worktree="/w",
            model_id="mistral/mistral-large", agent="ai-ops-readonly",
            role="scout", job_id="j", prompt="MSG")
        self.assertEqual(argv, ["/usr/bin/mistral", "run", "--json",
                                "--model", "mistral-large", "MSG"])


class EffortContract(unittest.TestCase):
    """Effort is measured per provider, refused when unmeasured, and never
    silently dropped."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def _argv(self, name, **over):
        base = dict(provider_argv=["/BIN"], worktree="/w", model_id="p/m",
                    agent="ai-ops-readonly", role="scout", job_id="j", prompt="MSG")
        base.update(over)
        return get_adapter(name).argv(**base)

    def test_status_is_from_the_declared_set(self):
        """`unsupported` and `unmeasured` remain distinct states even though the
        VALUES moved to the registry: a provider with no effort control at all is
        a different fact from a model nobody has measured."""
        from ai_ops.adapters import (
            EFFORT_SUPPORTED, EFFORT_UNMEASURED, EFFORT_UNSUPPORTED, _ADAPTERS,
        )

        allowed = {EFFORT_SUPPORTED, EFFORT_UNMEASURED, EFFORT_UNSUPPORTED}
        for name, adapter in _ADAPTERS.items():
            self.assertIn(adapter.effort_support()["status"], allowed, name)

    def test_a_provider_with_no_effort_control_defaults_to_unsupported(self):
        """The base class default, so a new harness cannot have an effort control
        invented for it by omission."""
        from ai_ops.adapters import EFFORT_UNSUPPORTED, ProviderAdapter

        self.assertEqual(ProviderAdapter().effort_support()["status"],
                         EFFORT_UNSUPPORTED)

    def test_adapters_declare_mechanism_only(self):
        """Effort VALUES are a per-model fact and live in the registry. An
        adapter that carried a value list would be wrong for some model in its
        own pool, and wrong silently."""
        from ai_ops.adapters import _ADAPTERS

        for name, adapter in _ADAPTERS.items():
            sup = adapter.effort_support()
            self.assertNotIn("values", sup,
                             f"{name} carries a provider-level value list")
            self.assertIn("flag", sup)
            self.assertIn(sup["validates"], {"client", "api", "none"}, name)

    def test_opencode_is_flagged_as_not_validating(self):
        """Measured: `--variant not-a-real-value` was accepted and the job ran to
        completion at full price. For this provider the rail is the only thing
        that can catch a bad value, so the adapter has to say so."""
        from ai_ops.adapters import _ADAPTERS

        self.assertEqual(_ADAPTERS["opencode"].effort_support()["validates"], "none")

    def test_the_registry_holds_per_model_sets_that_genuinely_differ(self):
        """The measurement that forced this shape: one provider, two models, two
        different sets. gpt-5.6-codex accepts `minimal` live; gpt-5.6-sol refuses
        it and enumerated the rest in its own error."""
        from ai_ops.registry import effort_values, model_record

        sol = effort_values(model_record("codex/gpt-5.6-sol"))
        codex_m = effort_values(model_record("codex/gpt-5.6-codex"))
        self.assertIsNotNone(sol)
        self.assertIsNotNone(codex_m)
        self.assertNotIn("minimal", sol)
        self.assertIn("minimal", codex_m)
        self.assertNotEqual(sol, codex_m)

    def test_every_measured_model_records_how_it_was_measured(self):
        """An auditor must be able to weigh a value set, not just read it."""
        from ai_ops.registry import effort_values, load_models

        for mid, rec in (load_models().get("models") or {}).items():
            if effort_values(rec):
                self.assertTrue(rec.get("effort_source"),
                                f"{mid} has effort_values with no effort_source")

    def test_an_uncurated_model_has_no_effort_set(self):
        """Identity can be derived from an id by rule; an accepted-value set
        cannot. Unmeasured is the fail-closed default."""
        from ai_ops.registry import effort_values, model_record

        self.assertIsNone(effort_values(model_record("grok/grok-9.9-invented")))

    def test_each_provider_carries_effort_in_its_own_form(self):
        cl = self._argv("claude", effort="high")
        self.assertEqual(cl[cl.index("--effort") + 1], "high")

        gk = self._argv("grok", effort="high")
        self.assertEqual(gk[gk.index("--reasoning-effort") + 1], "high")

        oc = self._argv("opencode", effort="high")
        self.assertEqual(oc[oc.index("--variant") + 1], "high")

        # Codex takes a config override, as one -c pair.
        cx = self._argv("codex", effort="high")
        self.assertEqual(cx[cx.index("-c") + 1], "model_reasoning_effort=high")

    def test_the_prompt_stays_last_when_effort_is_added(self):
        """opencode and codex both place the prompt positionally; an inserted
        flag that displaced it would make the effort value read as the task."""
        for name in ("opencode", "codex"):
            self.assertEqual(self._argv(name, effort="high")[-1], "MSG", name)

    def test_no_effort_means_no_flag(self):
        for name in ("claude", "grok", "opencode", "codex"):
            argv = self._argv(name)
            for token in ("--effort", "--reasoning-effort", "--variant"):
                self.assertNotIn(token, argv, f"{name} emitted {token} unasked")
            self.assertNotIn("model_reasoning_effort=", " ".join(argv), name)


class EffortResolution(unittest.TestCase):
    """The refusal path. Measured: OpenCode ACCEPTS an unrecognised effort value
    and runs the job to completion at full price, so a value the rail cannot
    verify must never be sent."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "python"))

    def _resolve(self, requested, provider, model_id):
        from ai_ops.job import _resolve_effort
        from ai_ops.registry import model_record

        return _resolve_effort(requested, get_adapter(provider), model_record(model_id))

    def test_no_request_is_not_a_refusal(self):
        self.assertIsNone(self._resolve(None, "claude", "claude/claude-sonnet-5"))
        self.assertIsNone(self._resolve(None, "opencode", "opencode-go/glm-5.3"))

    def test_a_measured_value_passes_through(self):
        self.assertEqual(
            self._resolve("xhigh", "claude", "claude/claude-sonnet-5"), "xhigh")
        self.assertEqual(self._resolve("high", "grok", "grok/grok-4.5"), "high")
        self.assertEqual(self._resolve("low", "codex", "codex/gpt-5.6-sol"), "low")

    def test_a_value_outside_the_model_set_is_refused(self):
        from ai_ops.errors import Refuse

        with self.assertRaises(Refuse) as ctx:
            self._resolve("ultra", "claude", "claude/claude-sonnet-5")
        self.assertIn("ultra", str(ctx.exception))
        self.assertIn("low", str(ctx.exception), "refusal must name what IS accepted")

    def test_the_same_value_can_be_valid_for_one_model_and_not_another(self):
        """The whole reason this is per model. Same provider, same flag."""
        from ai_ops.errors import Refuse

        self.assertEqual(
            self._resolve("minimal", "codex", "codex/gpt-5.6-codex"), "minimal")
        with self.assertRaises(Refuse) as ctx:
            self._resolve("minimal", "codex", "codex/gpt-5.6-sol")
        self.assertIn("gpt-5.6-sol", str(ctx.exception))

    def test_an_unmeasured_model_is_refused_with_a_remedy(self):
        from ai_ops.errors import Refuse

        with self.assertRaises(Refuse) as ctx:
            self._resolve("high", "opencode", "opencode-go/deepseek-v4-flash")
        msg = str(ctx.exception)
        self.assertIn("deepseek-v4-flash", msg, "the refusal must name the model")
        self.assertIn("per-MODEL", msg, "and say why it cannot be inferred")
        self.assertIn("registry.json", msg, "and where to record it")


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
