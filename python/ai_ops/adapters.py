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
    ) -> list[str]:
        # No --dir: grok works from cwd, and build_bwrap_argv --chdir's to the
        # worktree. No permission flags either -- deliberately. Grok has
        # --allow/--deny, but their rule syntax has not been probed, and its own
        # trust gate is fail-open (measured: "Project trusted: yes" with no
        # config at all), so the OS boundary is the control here, as the posture
        # already demands. Provider-side rules can be added as defense in depth
        # once their syntax is captured rather than guessed.
        wire = model_id.split("/", 1)[1] if "/" in model_id else model_id
        return list(provider_argv) + [
            "-p",
            prompt,
            "--output-format",
            "streaming-json",
            "--model",
            wire,
        ]

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

    def session_id(self, events: Iterable[dict[str, Any]]) -> str | None:
        for ev in events:
            if ev.get("type") == "end" and isinstance(ev.get("sessionId"), str):
                return ev["sessionId"]
        return None

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        if require_handoff:
            return None, "grok bounded-write is not supported yet (readonly roles only)"
        text = raw.decode("utf-8", "replace")
        parsed, _ = parse_lenient(text)
        norm = self.normalize(parsed, run_ended=True)
        fin = next((n for n in norm if n["event"] == "finished"), None)
        if fin is None:
            return None, "no terminal provider event"
        if fin["status"] == TERMINAL_FAILED:
            return None, fin.get("exitSummary") or "provider run failed"
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


_ADAPTERS = {"opencode": OpenCodeAdapter(), "grok": GrokAdapter()}


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
