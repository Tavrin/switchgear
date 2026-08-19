"""Grok CLI.

Moved out of adapters.py unchanged; see harnesses/base.py."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .base import EFFORT_SUPPORTED, ProviderAdapter, TERMINAL_FAILED
from .normalization import (
    _clip,
    _error_message,
    _extract_handoff,
    _extract_review,
    _finish_events,
    _num,
    parse_lenient,
)


class GrokAdapter(ProviderAdapter):
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

        from ..credentials import load_sandbox_session

        session = load_sandbox_session(provider_id, prec)
        d = _os.path.join(synth_home, ".grok")
        _os.makedirs(d, exist_ok=True)
        dest = _os.path.join(d, "auth.json")
        # Write private, then confirm the refresh token really is gone -- a
        # belt-and-braces check on the anti-leak invariant at the point the file
        # actually lands in the sandbox.
        #
        # This comment described a check that did not exist. It does now: this is
        # the ONE path in the rail where a real credential is written inside the
        # boundary, so the invariant is verified where the bytes land rather than
        # trusted from the strip upstream.
        with open(dest, "w", encoding="utf-8") as fh:
            _json.dump(session, fh)
        _os.chmod(dest, 0o600)

        from ..credentials import _has_refresh_token

        with open(dest, encoding="utf-8") as fh:
            landed = _json.load(fh)
        if _has_refresh_token(landed):
            _os.unlink(dest)
            from ..errors import Refuse

            raise Refuse(
                "refusing to run: a refresh token survived into the sandbox "
                "credential file. The written file has been removed and no job "
                "was started. This is the anti-leak invariant for the fallback "
                "credential tier."
            )
        return dest

    def effort_support(self) -> dict[str, Any]:
        """`--reasoning-effort` (alias `--effort`). Validated client-side by the
        CLI itself, which names its set in the error, so a bad value costs
        nothing. The MODEL may still refuse a value the CLI accepts."""
        return {"status": EFFORT_SUPPORTED, "flag": "--reasoning-effort",
                "validates": "client"}
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

    def session_store_paths(self) -> list[str]:
        """HOME-relative paths holding CONVERSATION state, not credentials.

        Provider session history lives inside the provider's HOME, and every
        job gets a fresh synthetic HOME that is reclaimed afterwards -- so
        without persisting these, a resumed job finds nothing and the CLI
        answers "No conversation found with session ID". Measured per
        provider; an empty list means resume is refused for it rather than
        silently starting a fresh conversation dressed as a continuation.

        Measured: ~/.grok/sessions/<url-encoded-cwd>/<session-id>/. Narrow on
        purpose -- ~/.grok also holds auth.json, which must never be persisted.

        Its session id still only appears in the TERMINAL event, so a Grok job
        that dies mid-run remains unresumable; that is a stream property, not a
        storage one.
        """
        return [".grok/sessions"]

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
        resume_session: str | None = None,
        effort: str | None = None,
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
        if effort:
            argv += ["--reasoning-effort", effort]
        if resume_session:
            argv += ["--resume", resume_session]
        if agent.endswith("bounded-write"):
            # Headless has no one to answer an approval prompt. The OS boundary
            # is the control: only the leased worktree is writable, the git dir
            # is read-only, and there is no network but the brokered upstream.
            argv.append("--always-approve")
        return argv

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "switchgear-bounded-write" if mode == "bounded-write" else "switchgear-readonly"

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
        from ..env import allowlisted_env, assert_no_host_secrets

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
        expiry, with switchgear never touching the refresh token."""
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


