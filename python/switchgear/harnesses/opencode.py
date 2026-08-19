"""OpenCode CLI: `opencode run --format json`.

Moved out of adapters.py unchanged; see harnesses/base.py."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .base import EFFORT_SUPPORTED, ProviderAdapter
from .normalization import (
    _clip,
    _error_message,
    _finish_events,
    _num,
    _tool_target,
)
from ..errors import ProviderError


class OpenCodeAdapter(ProviderAdapter):
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
    def effort_support(self) -> dict[str, Any]:
        """`--variant`. Mechanism only; values are per model in the registry.

        Flagged `validates: none` because it was MEASURED not to: an unknown
        value ran to completion at full price rather than being reported. For
        this provider a wrong value is invisible, which is why the rail must be
        the one to catch it.
        """
        return {"status": EFFORT_SUPPORTED, "flag": "--variant", "validates": "none"}
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

    def session_store_paths(self) -> list[str]:
        """HOME-relative paths holding CONVERSATION state, not credentials.

        Provider session history lives inside the provider's HOME, and every
        job gets a fresh synthetic HOME that is reclaimed afterwards -- so
        without persisting these, a resumed job finds nothing and the CLI
        answers "No conversation found with session ID". Measured per
        provider; an empty list means resume is refused for it rather than
        silently starting a fresh conversation dressed as a continuation.

        Measured: sessions live in a SQLite db at
        ~/.local/share/opencode/opencode.db (plus -wal/-shm and snapshot/).

        NOTE this directory is credential-ADJACENT: on the host it also holds
        auth.json. Our sandbox never has a real OpenCode credential (full tier,
        placeholder only), and job.py additionally refuses to bind a session
        store that contains anything credential-shaped -- a guard, because
        "it cannot happen today" is not a property, it is an observation.
        """
        return [".local/share/opencode"]

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
        resume = ["--session", resume_session] if resume_session else []
        return list(provider_argv) + [
            "run",
            *resume,
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
            f"switchgear {role} {job_id}",
        ] + (["--variant", effort] if effort else []) + [prompt]

    def version_argv(self, provider_argv: list[str]) -> list[str]:
        return list(provider_argv) + ["--version"]

    def agent_name(self, mode: str) -> str:
        return "switchgear-bounded-write" if mode == "bounded-write" else "switchgear-readonly"

    def isolation_env(
        self, synth_home: str, runtime: dict[str, Any], broker_base_url: str | None = None
    ) -> dict[str, str]:
        from ..provider import isolation_env as opencode_isolation_env

        return opencode_isolation_env(synth_home, runtime)

    def validate_result(self, raw: bytes, *, require_handoff: bool):
        """(handoff, error). OpenCode's strict parser also checks the handoff
        schema for bounded-write, so it stays the authority for this provider."""
        from .. import events as _events
        from ..errors import ProviderError, Refuse

        try:
            term = _events.parse_event_stream(raw, require_handoff=require_handoff)
            return term.get("_handoff"), None
        except (ProviderError, Refuse) as exc:
            return None, str(exc)

    def broker_runtime(self, runtime: dict[str, Any], base_url: str, model_id: str):
        """OpenCode is redirected through its CONFIG, not its environment."""
        from ..provider import runtime_with_broker

        return runtime_with_broker(runtime, base_url, model_id)

    def compose_prompt(self, prompt: str, instructions: str) -> str:
        """Unchanged: OpenCode reads the instructions from its agent file."""
        return prompt

    def extract_review(self, raw: bytes):
        from ..events import extract_review_verdict

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


