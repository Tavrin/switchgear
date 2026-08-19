"""Claude Code CLI.

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


class ClaudeCodeAdapter(ProviderAdapter):
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

    def effort_support(self) -> dict[str, Any]:
        """`--effort`, enumerated in the CLI's own --help."""
        return {"status": EFFORT_SUPPORTED, "flag": "--effort", "validates": "client"}
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

    def session_store_paths(self) -> list[str]:
        """HOME-relative paths holding CONVERSATION state, not credentials.

        Provider session history lives inside the provider's HOME, and every
        job gets a fresh synthetic HOME that is reclaimed afterwards -- so
        without persisting these, a resumed job finds nothing and the CLI
        answers "No conversation found with session ID". Measured per
        provider; an empty list means resume is refused for it rather than
        silently starting a fresh conversation dressed as a continuation.

        Measured: a run writes
        ~/.claude/projects/<cwd-slug>/<session-id>.jsonl inside the sandbox HOME.
        """
        return [".claude/projects", ".claude/sessions"]

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
        if effort:
            argv += ["--effort", effort]
        if resume_session:
            argv += ["--resume", resume_session]
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
        return "switchgear-bounded-write" if mode == "bounded-write" else "switchgear-readonly"

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        """Synthetic HOME does the isolating; the broker redirect is the only env.

        Measured inside the real sandbox: ~/.claude, ~/.claude.json AND
        /etc/claude-code (managed policy) are all ENOENT, so no host CLAUDE.md,
        settings, hooks, MCP servers or plugins can reach the job.
        """
        from ..env import allowlisted_env, assert_no_host_secrets

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


