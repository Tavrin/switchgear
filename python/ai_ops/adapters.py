"""Provider adapters: the seam where provider-specific shapes stop.

Everything above this module -- the digest, `logs`, `status`, and anything
atelier consumes -- works on a NORMALIZED vocabulary. Everything below it is one
provider's private format. That is the structural advantage over `the old Codex wrapper`,
which passes Codex's raw format through and would need the digest written once
per provider.

The normalized vocabulary, from docs/OBSERVABILITY.md:

    {"event": "status",   "sessionId": str}
    {"event": "tool",     "name": str, "target": str}
    {"event": "text",     "content": str}          # truncated at write time
    {"event": "finished", "status": str, "turns": int,
                          "costUSD": float, "tokens": int, "exitSummary": str}

`status` is one of completed / completed_empty / needs_input. `needs_input` is
load-bearing for atelier: it parks the ticket back to the operator rather than
recording a failure.

`sessionId` is the single most load-bearing field: without a durable session
identifier there is no resume, and a reply to a finished job cannot exist.

`changed_files` is deliberately NOT part of this vocabulary. Atelier derives the
result manifest from git itself and validates the landed tree at merge; it never
trusts an agent-reported file list. The rail keeps its own freeze delta for its
own gates -- that is a different thing, computed by the controller from the
worktree, not reported by the worker.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .errors import ProviderError

# Atelier truncates emitted lines at 400 chars (dispatch.mjs:4711). Match it, and
# bound at WRITE time rather than summarising after: a bound applied later has
# already let the full text through whatever was in between.
TEXT_LIMIT = 400

TERMINAL_COMPLETED = "completed"
TERMINAL_EMPTY = "completed_empty"
TERMINAL_NEEDS_INPUT = "needs_input"
# Not one of atelier's three success outcomes. A stream that stopped without a
# terminal event is the "claims done, evidence truncated" case, which their
# tripwires treat as suspicious -- over-reporting truncation is the right
# default, so this never reports as completed.
TERMINAL_FAILED = "failed"


def parse_lenient(text: str) -> tuple[list[dict[str, Any]], int]:
    """Decode as many whole JSON values as the text contains, and stop.

    Returns (events, consumed). Deliberately does NOT raise on a trailing
    partial value: projections read the evidence file WHILE it is being written,
    so the last object is routinely half-flushed. The strict parser in events.py
    still guards the result path, where trailing garbage is a real fault.
    """
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except ValueError:
            break  # incomplete tail; whatever precedes it is still good
        if not isinstance(obj, dict):
            break
        out.append(obj)
        idx = end
    return out, idx


class OpenCodeAdapter:
    """OpenCode 1.18.x.

    Every field below was read off a real captured stream
    (tests/fixtures/opencode-real-scout.jsonl), not from documentation and not
    from the mock. The mock previously invented a vocabulary the provider never
    emitted and 64 tests validated the fiction, so for this module the fixture is
    the authority and the mock is the thing under suspicion.

    Measured shapes:
      step_start  part{id,messageID,sessionID,snapshot,type:"step-start"}
      tool_use    part{type:"tool",tool,callID,state{status,input,output,...}}
      text        part{type:"text",text,time{...}}
      step_finish part{reason,tokens{total,input,output,cache{...}},cost}
      error       message | error{data{message},name}

    `sessionID` -- capital ID -- is present on EVERY event at top level, not on a
    dedicated status event as the design straw man assumed.
    """

    name = "opencode"
    # Full tier: the credential stays in the broker; the sandbox gets a
    # placeholder. Never write a real token into an OpenCode sandbox.
    credential_in_sandbox = False

    # The invocation. Kept here rather than in job.py so that the OpenCode-shaped
    # argv, the OPENCODE_* environment and the generated agent definition all sit
    # behind the same seam as parse() -- otherwise the seam is nominal and the
    # next provider still has to edit the job lifecycle.
    def required_flags(self) -> list[str]:
        """CLI surface this adapter's argv depends on.

        `providers verify` checks a NEW build still offers these, which is a
        real contract check rather than trust in a version number -- and it
        costs nothing, so it can run on every self-update.
        """
        return ["run", "--pure", "--dir", "--model", "--agent", "--format"]

    def list_models_argv(self, provider_argv: list[str]) -> list[str] | None:
        """How to ask this CLI what models it can actually serve.

        None means it offers no such command -- reported honestly rather
        than guessed, since a stale hardcoded list is the problem this
        exists to avoid.
        """
        return list(provider_argv) + ["models"]

    def argv(
        self,
        *,
        provider_argv: list[str],
        worktree: str,
        model_id: str,
        agent: str,
        role: str,
        job_id: str,
        prompt: str,
        attach_dir: str | None = None,
    ) -> list[str]:
        return list(provider_argv) + [
            "run",
            "--pure",
            "--dir",
            worktree,
            "--model",
            model_id,
            "--agent",
            agent,
            "--format",
            "json",
            "--title",
            f"ai-opencode {role} {job_id}",
            prompt,
        ]

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "ai-ops-bounded-write" if mode == "bounded-write" else "ai-ops-readonly"

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        from .provider import isolation_env as opencode_isolation_env

        return opencode_isolation_env(synth_home, runtime)

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        """(handoff, error). OpenCode's strict parser also checks the handoff
        schema for bounded-write, so it stays the authority for this provider."""
        from . import events as _events
        from .errors import ProviderError, Refuse

        try:
            term = _events.parse_event_stream(raw, require_handoff=require_handoff)
            return term.get("_handoff"), None
        except (ProviderError, Refuse) as exc:
            return None, str(exc)

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """OpenCode is redirected through its CONFIG, not its environment."""
        from .provider import runtime_with_broker

        return runtime_with_broker(runtime, base_url, model_id)

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """Unchanged: OpenCode reads the instructions from its agent file."""
        return prompt

    def extract_review(self, raw: bytes):
        from .events import extract_review_verdict

        return extract_review_verdict(raw)

    # A step_finish carries the reason the step ended. "stop" ends the run;
    # "tool-calls" only ends a step and more will follow.
    TERMINAL_REASONS = {"stop", "length", "content-filter"}

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            sid = ev.get("sessionID")
            if isinstance(sid, str) and sid:
                return sid
            part = ev.get("part")
            if isinstance(part, dict) and isinstance(part.get("sessionID"), str):
                return part["sessionID"]
        return None

    def normalize(
        self, events: Iterable[dict[str, Any]], *, run_ended: bool = False
    ) -> list[dict[str, Any]]:
        """Project a raw stream onto the normalized vocabulary.

        `run_ended` is the caller's knowledge, not the stream's: the same partial
        stream means "still working" during a job and "truncated" after one. A
        normalizer that guessed would either alarm on every healthy mid-run poll
        or stay silent on a genuinely cut-off run.
        """
        events = list(events)
        out: list[dict[str, Any]] = []
        sid = self.session_id(events)
        if sid:
            out.append({"event": "status", "sessionId": sid})

        turns = 0
        cost = 0.0
        tokens = 0
        last_text = ""
        errored = None
        saw_terminal = False

        for ev in events:
            ty = ev.get("type")
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            if ty == "step_start":
                turns += 1
            elif ty == "tool_use":
                out.append(
                    {
                        "event": "tool",
                        "name": str(part.get("tool") or "?"),
                        "target": _tool_target(part),
                    }
                )
            elif ty == "text":
                content = str(part.get("text") or ev.get("text") or "")
                if content:
                    last_text = content
                    out.append({"event": "text", "content": _clip(content)})
            elif ty == "step_finish":
                cost += _num(part.get("cost"))
                tokens += int(_num((part.get("tokens") or {}).get("total")))
                if part.get("reason") in self.TERMINAL_REASONS:
                    saw_terminal = True
            elif ty == "error":
                errored = _error_message(ev)
            elif ty == "complete":
                # The committed mock's vocabulary. Real OpenCode never emits it.
                saw_terminal = True

        counters = {
            "turns": turns,
            "costUSD": round(cost, 8),
            "tokens": tokens,
        }
        out.extend(
            _finish_events(
                counters,
                saw_terminal=saw_terminal,
                run_ended=run_ended,
                errored=errored,
                last_text=last_text,
            )
        )
        return out


class GrokAdapter:
    """Grok CLI 1.0.x, headless (`-p --output-format streaming-json`).

    Every shape below was read off a real captured stream
    (tests/fixtures/grok-real-scout.jsonl, 2026-08-18, grok-4.6-build) -- never
    from documentation. Three ways this dialect differs from OpenCode's, each of
    which would have broken a doc-written normalizer:

    - `text` and `thought` arrive as DELTAS, often one token per event. They
      must be coalesced; emitting one normalized event per fragment would turn a
      one-sentence answer into hundreds of events.
    - `sessionId` exists ONLY in the terminal `end` event. Mid-run there is no
      session identifier in the stream at all, so resume-after-crash has nothing
      to key on until the run closes. Reported honestly as None until then.
    - Cost and turns arrive pre-totalled on `end` (`total_cost_usd`,
      `num_turns`); mid-run the only counters are the per-model-call `usage`
      events, whose count matches num_turns in the capture.

    Measured event types: available_commands (session preamble, ignored),
    thought{data} / text{data} deltas, usage{usage{...},signature},
    tool_call{toolCallId,toolName,rawInput,...}, tool_call_update, and
    end{stopReason,sessionId,requestId,usage,num_turns,total_cost_usd,modelUsage}.

    `thought` content is deliberately NOT emitted into the digest: reasoning
    deltas belong to the full stream for a human, not in a projection that lands
    in a parent agent's context. Its volume is visible via reasoning tokens.
    """

    name = "grok"
    # FALLBACK tier: Grok validates its session locally and rejects any
    # placeholder (measured, four ways), so its access token must be in the
    # sandbox. The refresh token is stripped before it is written; --unshare-net
    # and the broker's path/model/ceiling still bound what the job can do with
    # the token, so it can be USED for the job but has no channel out except the
    # one brokered upstream.
    credential_in_sandbox = True

    def write_sandbox_credential(self, synth_home: str, provider_id: str, prec: dict) -> str:
        import json as _json
        import os as _os

        from .credentials import load_sandbox_session

        session = load_sandbox_session(provider_id, prec)
        d = _os.path.join(synth_home, ".grok")
        _os.makedirs(d, exist_ok=True)
        dest = _os.path.join(d, "auth.json")
        # Write private, then confirm the refresh token really is gone -- a
        # belt-and-braces check on the anti-leak invariant at the point the file
        # actually lands in the sandbox.
        with open(dest, "w", encoding="utf-8") as fh:
            _json.dump(session, fh)
        _os.chmod(dest, 0o600)
        return dest

    def required_flags(self) -> list[str]:
        """CLI surface this adapter's argv depends on.

        `providers verify` checks a NEW build still offers these, which is a
        real contract check rather than trust in a version number -- and it
        costs nothing, so it can run on every self-update.
        """
        return ["--output-format", "--model", "--always-approve"]

    def list_models_argv(self, provider_argv: list[str]) -> list[str] | None:
        """How to ask this CLI what models it can actually serve.

        None means it offers no such command -- reported honestly rather
        than guessed, since a stale hardcoded list is the problem this
        exists to avoid.
        """
        return list(provider_argv) + ["models"]

    def argv(
        self,
        *,
        provider_argv: list[str],
        worktree: str,
        model_id: str,
        agent: str,
        role: str,
        job_id: str,
        prompt: str,
        attach_dir: str | None = None,
    ) -> list[str]:
        # No --dir: grok works from cwd, and build_bwrap_argv --chdir's to the
        # worktree. No permission flags either -- deliberately. Grok has
        # --allow/--deny, but their rule syntax has not been probed, and its own
        # trust gate is fail-open (measured: "Project trusted: yes" with no
        # config at all), so the OS boundary is the control here, as the posture
        # already demands. Provider-side rules can be added as defense in depth
        # once their syntax is captured rather than guessed.
        wire = model_id.split("/", 1)[1] if "/" in model_id else model_id
        argv = list(provider_argv) + [
            "-p",
            prompt,
            "--output-format",
            "streaming-json",
            "--model",
            wire,
        ]
        if agent.endswith("bounded-write"):
            # Headless has no one to answer an approval prompt. The OS boundary
            # is the control: only the leased worktree is writable, the git dir
            # is read-only, and there is no network but the brokered upstream.
            argv.append("--always-approve")
        return argv

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "ai-ops-bounded-write" if mode == "bounded-write" else "ai-ops-readonly"

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        """Grok needs no config file: the synthetic HOME alone isolates it.

        Measured by probe -- host CLAUDE.md, 375 permission rules, 46 skills,
        plugins, MCP and LSP servers all resolve to zero inside. So the only env
        this adds is the broker redirect, and only when there is a broker.

        Both knobs were confirmed by pointing GROK_CLI_BASE_URL at a recording
        server: the CLI sent every request there, and sent an Authorization
        header built from GROK_AUTH_PROVIDER_ACCESS_TOKEN. The token here is the
        same placeholder every provider gets -- the broker overwrites the header
        upstream, so the real session never enters the sandbox.
        """
        from .env import allowlisted_env, assert_no_host_secrets

        # No GROK_AUTH_PROVIDER_ACCESS_TOKEN: auth comes from the session file
        # written by write_sandbox_credential (fallback tier). Setting a
        # placeholder token here would override the file and fail Grok's local
        # validation -- measured. Only the broker redirect belongs in the env.
        extra: dict[str, str] = {}
        if broker_base_url:
            base = broker_base_url.rstrip("/")
            extra["GROK_CLI_BASE_URL"] = base
            extra["GROK_MODELS_BASE_URL"] = base
        env = allowlisted_env(home=synth_home, extra=extra)
        assert_no_host_secrets(env)
        return env

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """Grok is redirected by ENVIRONMENT, not by config, so the runtime dict
        is returned untouched and isolation_env does the work."""
        return runtime

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """No agent-file mechanism: the role instructions go in the prompt.

        Same text the OpenCode agent file carries (policy.role_instructions), so
        the handoff contract a worker is told and the one the rail validates
        cannot drift apart.
        """
        if not instructions:
            return prompt
        return f"{instructions.strip()}\n\n---\n\n{prompt}"


    def extract_review(self, raw: bytes):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        return _extract_review(self.full_text(parsed))

    def full_text(self, events: Iterable[dict[str, Any]]) -> str:
        """Unclipped text, coalesced from the per-token deltas."""
        return "".join(
            str(ev.get("data") or "") for ev in events if ev.get("type") == "text"
        )

    def refresh_argv(self, provider_argv: list[str]) -> list[str] | None:
        """`grok models` refreshes an expired session in place -- measured:
        backdating expires_at then running it produced a NEW token and a new
        expiry, with agent-ops never touching the refresh token."""
        return list(provider_argv) + ["models"]

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            if ev.get("type") == "end" and isinstance(ev.get("sessionId"), str):
                return ev["sessionId"]
        return None

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        norm = self.normalize(parsed, run_ended=True)
        fin = next((n for n in norm if n["event"] == "finished"), None)
        if fin is None:
            return None, "no terminal provider event"
        if fin["status"] == TERMINAL_FAILED:
            return None, fin.get("exitSummary") or "provider run failed"
        if require_handoff:
            return _extract_handoff(self.full_text(parsed))
        return None, None

    def normalize(
        self, events: Iterable[dict[str, Any]], *, run_ended: bool = False
    ) -> list[dict[str, Any]]:
        events = list(events)
        out: list[dict[str, Any]] = []
        sid = self.session_id(events)
        if sid:
            out.append({"event": "status", "sessionId": sid})

        text_parts: list[str] = []
        model_calls = 0
        tokens_running = 0
        end_ev: dict[str, Any] | None = None
        errored: str | None = None

        for ev in events:
            ty = ev.get("type")
            if ty == "text":
                delta = ev.get("data")
                if isinstance(delta, str):
                    text_parts.append(delta)
            elif ty == "tool_call":
                out.append(
                    {
                        "event": "tool",
                        "name": str(ev.get("toolName") or ev.get("title") or "?"),
                        "target": _grok_tool_target(ev),
                    }
                )
            elif ty == "usage":
                model_calls += 1
                u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
                tokens_running += int(
                    _num(u.get("input_tokens"))
                    + _num(u.get("output_tokens"))
                    + _num(u.get("reasoning_tokens"))
                )
            elif ty == "end":
                end_ev = ev
            elif ty == "error":
                # Not observed in the capture; handled defensively so a future
                # error event degrades to `failed` rather than to silence.
                errored = _error_message(ev)

        last_text = "".join(text_parts)
        if last_text.strip():
            out.append({"event": "text", "content": _clip(last_text)})

        failure = None
        if end_ev is not None:
            stop = end_ev.get("stopReason")
            if stop != "end_turn":
                # Only end_turn was observed as a normal close. Anything else is
                # reported as a failure with the provider's own word for it --
                # guessing which unobserved reasons are benign would be the
                # invented-vocabulary mistake again.
                failure = f"provider stopped abnormally: {stop!r}"
            end_usage = end_ev.get("usage") if isinstance(end_ev.get("usage"), dict) else {}
            counters = {
                "turns": int(_num(end_ev.get("num_turns")) or model_calls),
                "costUSD": round(_num(end_ev.get("total_cost_usd")), 8),
                "tokens": int(_num(end_usage.get("total_tokens")) or tokens_running),
            }
        else:
            counters = {"turns": model_calls, "costUSD": 0.0, "tokens": tokens_running}

        out.extend(
            _finish_events(
                counters,
                saw_terminal=end_ev is not None,
                run_ended=run_ended,
                errored=errored,
                last_text=last_text,
                failure=failure,
            )
        )
        return out


def _grok_tool_target(ev: dict[str, Any]) -> str:
    args = ev.get("rawInput") if isinstance(ev.get("rawInput"), dict) else {}
    for key in ("target_file", "path", "file", "command", "pattern", "query", "url"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return _clip(val, 200)
    return ""


class ClaudeCodeAdapter:
    """Claude Code 2.1.x, headless (`-p --output-format stream-json --verbose`).

    Read off a real captured stream (tests/fixtures/claude-real-scout.jsonl),
    captured with a CLEAN HOME and the OAuth access token supplied via
    ANTHROPIC_AUTH_TOKEN -- i.e. exactly the full-tier shape this adapter uses,
    so the fixture is a recording of the real configuration, not an approximation.

    Measured shapes:
      system/init          {session_id, cwd, tools[...]}
      assistant            {message:{content:[{type:thinking|text|tool_use,...}]}}
      user                 {message:{content:[{type:tool_result,...}]}}
      result/success       {session_id,num_turns,total_cost_usd,usage{...},
                            stop_reason,is_error,result}
      rate_limit_event     {rate_limit_info{status,resetsAt,rateLimitType,...}}

    Differences from every other adapter, each of which would break a
    doc-written normalizer:

    - `session_id` is on EVERY event (snake_case), unlike Grok's `sessionId`
      which appears only in its terminal event.
    - Text is NOT deltas: an `assistant` event carries whole content blocks. The
      answer is the last `text` block, and `thinking` blocks sit beside it in the
      same array -- they must be skipped, not concatenated.
    - Cost and turns arrive pre-totalled on `result` (`total_cost_usd`,
      `num_turns`), and `is_error` is an explicit success flag rather than
      something to infer.
    """

    name = "claude"
    # Full tier: token via ANTHROPIC_AUTH_TOKEN, broker holds the real value.
    # Proven: a clean HOME with no credentials file runs fine on an env token.
    credential_in_sandbox = False

    def required_flags(self) -> list[str]:
        """CLI surface this adapter's argv depends on.

        `providers verify` checks a NEW build still offers these, which is a
        real contract check rather than trust in a version number -- and it
        costs nothing, so it can run on every self-update.
        """
        return ["--print", "--output-format", "--verbose", "--model", "--permission-mode"]

    def list_models_argv(self, provider_argv: list[str]) -> list[str] | None:
        """How to ask this CLI what models it can actually serve.

        None means it offers no such command -- reported honestly rather
        than guessed, since a stale hardcoded list is the problem this
        exists to avoid.
        """
        return None

    def argv(
        self,
        *,
        provider_argv: list[str],
        worktree: str,
        model_id: str,
        agent: str,
        role: str,
        job_id: str,
        prompt: str,
        attach_dir: str | None = None,
    ) -> list[str]:
        # No --dir: build_bwrap_argv --chdir's to the worktree. --verbose is
        # REQUIRED for stream-json (the CLI rejects the combination without it).
        wire = model_id.split("/", 1)[1] if "/" in model_id else model_id
        argv = list(provider_argv) + [
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            wire,
        ]
        if attach_dir:
            # Controller-written material (the frozen review diff) lives outside
            # the worktree, and Claude's tools refuse paths outside the working
            # directory -- measured: a review that answered "I don't have
            # permission to read the attached diff file". One extra directory,
            # written only by the controller.
            argv += ["--add-dir", attach_dir]
        if agent.endswith("bounded-write"):
            # Claude's own permission prompts cannot be answered in headless
            # mode. The OS boundary is the real control here -- the worktree is
            # the only writable mount and the git dir is read-only -- so its
            # in-process gate is redundant, not load-bearing.
            argv += ["--permission-mode", "acceptEdits"]
        return argv

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "ai-ops-bounded-write" if mode == "bounded-write" else "ai-ops-readonly"

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        """Synthetic HOME does the isolating; the broker redirect is the only env.

        Measured inside the real sandbox: ~/.claude, ~/.claude.json AND
        /etc/claude-code (managed policy) are all ENOENT, so no host CLAUDE.md,
        settings, hooks, MCP servers or plugins can reach the job.
        """
        from .env import allowlisted_env, assert_no_host_secrets

        extra: dict[str, str] = {}
        if broker_base_url:
            extra["ANTHROPIC_BASE_URL"] = broker_base_url.rstrip("/")
            # The OAuth path: this becomes `Authorization: Bearer <value>`.
            # ANTHROPIC_API_KEY would instead select the BYOK path and send
            # x-api-key -- measured, and the reason the broker has a per-provider
            # auth header at all.
            extra["ANTHROPIC_AUTH_TOKEN"] = "broker-placeholder-not-a-credential"
        env = allowlisted_env(home=synth_home, extra=extra)
        assert_no_host_secrets(env)
        return env

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """Redirected by ENVIRONMENT; the runtime dict is not used."""
        return runtime

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """No agent-file mechanism: the role instructions go in the prompt.

        Same text the OpenCode agent file carries (policy.role_instructions), so
        the handoff contract a worker is told and the one the rail validates
        cannot drift apart.
        """
        if not instructions:
            return prompt
        return f"{instructions.strip()}\n\n---\n\n{prompt}"

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            sid = ev.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return None

    def normalize(
        self, events: Iterable[dict[str, Any]], *, run_ended: bool = False
    ) -> list[dict[str, Any]]:
        events = list(events)
        out: list[dict[str, Any]] = []
        sid = self.session_id(events)
        if sid:
            out.append({"event": "status", "sessionId": sid})

        last_text = ""
        result_ev: dict[str, Any] | None = None
        errored: str | None = None

        for ev in events:
            ty = ev.get("type")
            if ty == "assistant":
                msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        last_text = str(block["text"])
                        out.append({"event": "text", "content": _clip(last_text)})
                    elif btype == "tool_use":
                        out.append(
                            {
                                "event": "tool",
                                "name": str(block.get("name") or "?"),
                                "target": _claude_tool_target(block),
                            }
                        )
                    # `thinking` blocks are deliberately dropped: reasoning
                    # belongs to the full stream for a human, not to a digest
                    # that lands in a parent agent's context.
            elif ty == "result":
                result_ev = ev
            elif ty == "error":
                errored = _error_message(ev)

        failure = None
        if result_ev is not None:
            if result_ev.get("is_error"):
                failure = str(
                    result_ev.get("result") or result_ev.get("subtype") or "provider reported error"
                )
            usage = result_ev.get("usage") if isinstance(result_ev.get("usage"), dict) else {}
            counters = {
                "turns": int(_num(result_ev.get("num_turns"))),
                "costUSD": round(_num(result_ev.get("total_cost_usd")), 8),
                "tokens": int(
                    _num(usage.get("input_tokens"))
                    + _num(usage.get("output_tokens"))
                    + _num(usage.get("cache_read_input_tokens"))
                    + _num(usage.get("cache_creation_input_tokens"))
                ),
            }
        else:
            counters = {"turns": 0, "costUSD": 0.0, "tokens": 0}

        out.extend(
            _finish_events(
                counters,
                saw_terminal=result_ev is not None,
                run_ended=run_ended,
                errored=errored,
                last_text=last_text,
                failure=failure,
            )
        )
        return out

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        norm = self.normalize(parsed, run_ended=True)
        fin = next((n for n in norm if n["event"] == "finished"), None)
        if fin is None:
            return None, "no terminal provider event"
        if fin["status"] == TERMINAL_FAILED:
            return None, fin.get("exitSummary") or "provider run failed"
        if require_handoff:
            # From the RAW blocks, not the normalized events: those are clipped
            # to 400 chars for the digest, and a handoff object is routinely
            # longer than that. Extracting from the clipped copy would reject
            # perfectly good work with "missing handoff".
            return _extract_handoff(self.full_text(parsed))
        return None, None

    def refresh_argv(self, provider_argv: list[str]) -> list[str] | None:
        return list(provider_argv) + ["doctor"]


    def extract_review(self, raw: bytes):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        return _extract_review(self.full_text(parsed))

    def full_text(self, events: Iterable[dict[str, Any]]) -> str:
        """Unclipped assistant text, for contract extraction."""
        out = []
        for ev in events:
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    out.append(str(block["text"]))
        return "\n".join(out)


def _claude_tool_target(block: dict[str, Any]) -> str:
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    for key in ("file_path", "path", "pattern", "command", "query", "url", "prompt"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return _clip(val, 200)
    return ""


class CodexAdapter:
    """Codex CLI 0.147.x, headless (`exec --json`).

    Read off a real captured stream (tests/fixtures/codex-real-scout.jsonl).
    Measured shapes -- a much smaller vocabulary than the others:

      thread.started   {thread_id}          <- the session id, on the FIRST event
      turn.started     {}
      item.started     {item:{id,type,...}}
      item.completed   {item:{id,type,...}} <- type agent_message{text}
                                              or command_execution{command,...}
      turn.completed   {usage{input_tokens,cached_input_tokens,output_tokens,
                              reasoning_output_tokens}}

    Two consequences worth stating because they differ from every sibling:

    - `thread_id` is on the FIRST event, not the last (Grok's sessionId is only
      on its terminal event). Resume information is therefore available from the
      moment the job starts.
    - Codex reports NO COST, only tokens. costUSD is 0.0 rather than invented,
      which is why an atelier lane would set reportsCost false for this provider.
    """

    name = "codex"
    # FULL tier: measured -- Codex accepts a placeholder session file and sends
    # that token as Bearer, so the broker holds the real one.
    credential_in_sandbox = False

    # A structurally valid but meaningless token. Codex reads its session file at
    # startup and puts the token in an Authorization header; the broker replaces
    # both that header and the ChatGPT-Account-ID derived from it.
    PLACEHOLDER_ACCOUNT = "00000000-0000-0000-0000-000000000000"

    def _placeholder_token(self) -> str:
        import base64 as _b64
        import time as _time

        def seg(d):
            return _b64.urlsafe_b64encode(
                json.dumps(d, separators=(",", ":")).encode()
            ).decode().rstrip("=")

        now = int(_time.time())
        return ".".join([
            seg({"alg": "RS256", "typ": "JWT"}),
            seg({
                "iss": "https://auth.openai.com", "aud": "broker-placeholder",
                "sub": self.PLACEHOLDER_ACCOUNT, "exp": now + 86400, "iat": now,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": self.PLACEHOLDER_ACCOUNT,
                    "chatgpt_plan_type": "placeholder",
                    "user_id": self.PLACEHOLDER_ACCOUNT,
                },
            }),
            "YnJva2VyLXBsYWNlaG9sZGVy",
        ])

    def required_flags(self) -> list[str]:
        """CLI surface this adapter's argv depends on.

        `providers verify` checks a NEW build still offers these, which is a
        real contract check rather than trust in a version number -- and it
        costs nothing, so it can run on every self-update.
        """
        return ["exec", "--json", "--sandbox", "--model", "--skip-git-repo-check"]

    def list_models_argv(self, provider_argv: list[str]) -> list[str] | None:
        """How to ask this CLI what models it can actually serve.

        None means it offers no such command -- reported honestly rather
        than guessed, since a stale hardcoded list is the problem this
        exists to avoid.
        """
        return None

    def argv(
        self,
        *,
        provider_argv: list[str],
        worktree: str,
        model_id: str,
        agent: str,
        role: str,
        job_id: str,
        prompt: str,
        attach_dir: str | None = None,
    ) -> list[str]:
        # --skip-git-repo-check: the sandbox mounts the git dir read-only and
        # Codex's own check is redundant with the rail's worktree identity work.
        wire = model_id.split("/", 1)[1] if "/" in model_id else model_id
        # Codex runs model-generated shell commands under its OWN sandbox, nested
        # inside ours. read-only for a scout; workspace-write when the job is
        # allowed to edit. Ours remains the real boundary either way -- for a
        # readonly job the worktree is a read-only mount, so even a wrong value
        # here cannot grant writes.
        sandbox_mode = "workspace-write" if agent.endswith("bounded-write") else "read-only"
        argv = list(provider_argv) + [
            "exec", "--skip-git-repo-check", "--json",
            "--sandbox", sandbox_mode, "--model", wire,
        ]
        if attach_dir:
            argv += ["--add-dir", attach_dir]
        return argv + [prompt]

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "ai-ops-bounded-write" if mode == "bounded-write" else "ai-ops-readonly"

    def refresh_argv(self, provider_argv: list[str]) -> list[str] | None:
        """A cheap, no-model command that makes the CLI refresh its own session."""
        return list(provider_argv) + ["doctor"]

    def extra_binds(self, provider_argv: list[str]) -> list[str]:
        """Codex is not one file: it needs its sibling helpers.

        Binding only the executable produced a job that authenticated, streamed
        and answered -- while telling the user "the workspace execution tool is
        unavailable", because codex-code-mode-host was missing. A provider that
        cannot read a file is useless for scout or review, so the whole vendor
        directory (bin/, codex-path/ with its bundled rg, codex-resources/) is
        bound read-only.
        """
        import os as _os

        out = []
        for arg in provider_argv:
            if _os.path.isabs(arg) and _os.path.exists(arg):
                # .../vendor/<triple>/bin/codex -> .../vendor/<triple>
                vendor = _os.path.dirname(_os.path.dirname(_os.path.realpath(arg)))
                if _os.path.isdir(_os.path.join(vendor, "bin")):
                    out.append(vendor)
        return out

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        """Write Codex's own config + a placeholder session into the sandbox.

        CODEX_HOME defaults to $HOME/.codex, so the synthetic HOME already
        isolates it (measured: host auth, 3 MCP servers, 1,173 rollouts and the
        state DB are all absent inside). It is still set explicitly rather than
        relying on that default holding.

        `supports_websockets = false` is load-bearing: by default Codex reaches
        inference over `wss://api.openai.com/v1/responses`, which IGNORES the
        base-url redirect and which an HTTP broker cannot proxy at all. Forcing
        the HTTP transport is what makes this provider brokerable.
        """
        import os as _os

        from .env import allowlisted_env, assert_no_host_secrets

        codex_home = _os.path.join(synth_home, ".codex")
        _os.makedirs(codex_home, exist_ok=True)
        extra = {"CODEX_HOME": codex_home}
        if broker_base_url:
            base = broker_base_url.rstrip("/")
            with open(_os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write(
                    'model_provider = "broker"\n'
                    f'chatgpt_base_url = "{base}"\n'
                    "[model_providers.broker]\n"
                    'name = "broker"\n'
                    f'base_url = "{base}"\n'
                    'wire_api = "responses"\n'
                    "requires_openai_auth = true\n"
                    "supports_websockets = false\n"
                )
            auth = _os.path.join(codex_home, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "OPENAI_API_KEY": None,
                        "tokens": {
                            "id_token": self._placeholder_token(),
                            "access_token": self._placeholder_token(),
                            "account_id": self.PLACEHOLDER_ACCOUNT,
                        },
                        "last_refresh": "2026-01-01T00:00:00.000000000Z",
                    },
                    fh,
                )
            _os.chmod(auth, 0o600)
        env = allowlisted_env(home=synth_home, extra=extra)
        assert_no_host_secrets(env)
        return env

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """Redirected by its config file, written in isolation_env."""
        return runtime

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """No agent-file mechanism: the role instructions go in the prompt.

        Same text the OpenCode agent file carries (policy.role_instructions), so
        the handoff contract a worker is told and the one the rail validates
        cannot drift apart.
        """
        if not instructions:
            return prompt
        return f"{instructions.strip()}\n\n---\n\n{prompt}"


    def extract_review(self, raw: bytes):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        return _extract_review(self.full_text(parsed))

    def full_text(self, events: Iterable[dict[str, Any]]) -> str:
        """Unclipped agent_message text, for contract extraction."""
        out = []
        for ev in events:
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                if item.get("text"):
                    out.append(str(item["text"]))
        return "\n".join(out)

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            if ev.get("type") == "thread.started" and isinstance(ev.get("thread_id"), str):
                return ev["thread_id"]
        return None

    def normalize(
        self, events: Iterable[dict[str, Any]], *, run_ended: bool = False
    ) -> list[dict[str, Any]]:
        events = list(events)
        out: list[dict[str, Any]] = []
        sid = self.session_id(events)
        if sid:
            out.append({"event": "status", "sessionId": sid})

        turns = 0
        last_text = ""
        usage: dict[str, Any] = {}
        saw_terminal = False
        errored: str | None = None

        for ev in events:
            ty = ev.get("type")
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if ty == "turn.started":
                turns += 1
            elif ty == "turn.completed":
                saw_terminal = True
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            elif ty in ("turn.failed", "error"):
                errored = _error_message(ev) if ty == "error" else str(
                    ev.get("error") or "turn failed"
                )
            elif ty == "item.completed":
                itype = item.get("type")
                if itype == "error":
                    # An error ITEM is Codex reporting that something it needed
                    # was unavailable. Measured: a run whose code-mode host was
                    # missing emitted one, then answered "I can't inspect the
                    # file" -- and still closed its turn normally, so the run
                    # read as `completed`. That is the claims-done-with-evidence-
                    # of-failure shape; record it as the failure it is.
                    errored = str(item.get("message") or "provider reported an error item")
                elif itype == "agent_message" and item.get("text"):
                    last_text = str(item["text"])
                    out.append({"event": "text", "content": _clip(last_text)})
                elif itype == "command_execution":
                    out.append({
                        "event": "tool",
                        "name": "command_execution",
                        "target": _clip(str(item.get("command") or ""), 200),
                    })
                elif itype:
                    out.append({
                        "event": "tool",
                        "name": str(itype),
                        "target": _clip(str(item.get("path") or item.get("id") or ""), 200),
                    })

        counters = {
            "turns": turns,
            # Codex reports NO cost, only tokens. Report 0.0 rather than invent a
            # number; the token counts are real.
            "costUSD": 0.0,
            "tokens": int(
                _num(usage.get("input_tokens"))
                + _num(usage.get("output_tokens"))
                + _num(usage.get("cached_input_tokens"))
                + _num(usage.get("reasoning_output_tokens"))
            ),
        }
        out.extend(
            _finish_events(
                counters,
                saw_terminal=saw_terminal,
                run_ended=run_ended,
                errored=errored,
                last_text=last_text,
            )
        )
        return out

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        parsed, _ = parse_lenient(raw.decode("utf-8", "replace"))
        fin = next(
            (n for n in self.normalize(parsed, run_ended=True) if n["event"] == "finished"), None
        )
        if fin is None:
            return None, "no terminal provider event"
        if fin["status"] == TERMINAL_FAILED:
            return None, fin.get("exitSummary") or "provider run failed"
        if require_handoff:
            return _extract_handoff(self.full_text(parsed))
        return None, None


def _extract_review(text: str):
    """Pull the reviewer's verdict object out of its final text.

    The mirror of _extract_handoff, and it exists for the same reason: only
    OpenCode emits structured objects of its own, so for every other provider the
    verdict has to be recovered from the model's own words.
    """
    from .events import extract_object

    payload = extract_object(text or "", "review")
    if not isinstance(payload, dict):
        raise ProviderError("reviewer produced no review object")
    verdict = payload.get("verdict")
    if verdict not in {"promote", "reject", "needs_changes"}:
        raise ProviderError("reviewer produced no explicit verdict")
    findings = payload.get("findings") or []
    if not isinstance(findings, list):
        raise ProviderError("findings must be a list")
    files = payload.get("reviewed_files") or []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ProviderError("reviewed_files must be a list of strings")
    return verdict, findings, files


def _extract_handoff(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Pull the mandatory handoff object out of a worker's final text.

    OpenCode's strict parser does this against its own event objects; every
    other provider only ever produces TEXT, so the handoff has to be recovered
    from the model's own words and then validated against the SAME schema. A
    write job without a schema-valid handoff is rejected -- the object is the
    worker's structured claim about what it did, and promotion binds to it.
    """
    from .events import extract_object
    from .schema import validate

    obj = extract_object(text or "", "handoff")
    if not isinstance(obj, dict):
        return None, "write job missing schema-valid handoff object"
    try:
        validate(obj, "handoff.schema.json")
    except Exception as exc:
        return None, f"handoff object failed schema validation: {exc}"
    if obj.get("status") not in {"awaiting_review", "complete"}:
        return None, "wrong handoff status"
    return obj, None


def _finish_events(
    counters: dict[str, Any],
    *,
    saw_terminal: bool,
    run_ended: bool,
    errored: str | None,
    last_text: str,
    failure: str | None = None,
) -> list[dict[str, Any]]:
    """The one honesty policy for closing a normalized stream, shared by every
    adapter so a new provider cannot quietly relax it.

    While the job runs: `progress` with counters and NO terminal event, so an
    adapter tailing for `finished` never transitions early. Once it is over:
    errors first; then truncation (`sawTerminal` false is never `completed` --
    "claims done, evidence truncated" is the case atelier tripwires on); then a
    provider-declared abnormal stop (`failure`); then empty-vs-completed by
    whether the model actually said anything.
    """
    if not (saw_terminal or run_ended):
        return [{"event": "progress", **counters}]

    if errored is not None:
        status = TERMINAL_NEEDS_INPUT if _is_input_request(errored) else TERMINAL_FAILED
        summary = errored
    elif not saw_terminal:
        status = TERMINAL_FAILED
        summary = (
            "stream truncated: the run ended without a terminal provider event, "
            "so this result is not evidence of completion"
        )
    elif failure:
        status = TERMINAL_FAILED
        summary = failure
    elif not last_text.strip():
        status = TERMINAL_EMPTY
        summary = ""
    else:
        status = TERMINAL_COMPLETED
        summary = _clip(last_text)

    return [
        {
            "event": "finished",
            "status": status,
            **counters,
            "exitSummary": summary,
            "sawTerminal": saw_terminal,
        }
    ]


def _tool_target(part: dict[str, Any]) -> str:
    """A short, human-meaningful subject for a tool call.

    Kept minimal on purpose: atelier renders tool events as plain text, so a
    large payload here buys nothing and costs context everywhere downstream.
    """
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    args = state.get("input") if isinstance(state.get("input"), dict) else {}
    for key in ("filePath", "path", "pattern", "file", "command", "query"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return _clip(val, 200)
    return ""


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _error_message(ev: dict[str, Any]) -> str:
    msg = ev.get("message")
    if not msg:
        err = ev.get("error")
        if isinstance(err, dict):
            msg = (err.get("data") or {}).get("message") or err.get("name")
    return str(msg or "provider error event")


def _is_input_request(message: str) -> bool:
    low = message.lower()
    return "permission" in low or "needs input" in low or "awaiting input" in low


_ADAPTERS = {
    "opencode": OpenCodeAdapter(),
    "grok": GrokAdapter(),
    "claude": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
}


def get_adapter(provider: str | None):
    """Resolve the CLI adapter for a profile's `provider` field.

    Note this is the PROVIDER BINARY (opencode), not the model pool
    (opencode-go, openrouter) that registry.provider_record resolves. Several
    pools are reached through one binary, so conflating them would pick the
    wrong adapter the moment a second pool appears.
    """
    from .errors import Refuse

    adapter = _ADAPTERS.get((provider or "").strip().lower())
    if adapter is None:
        raise Refuse(f"no adapter for provider {provider!r} (known: {sorted(_ADAPTERS)})")
    return adapter
