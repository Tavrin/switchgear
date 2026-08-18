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

        if not (saw_terminal or run_ended):
            # Still working. Emit counters WITHOUT a terminal event: an adapter
            # tailing the stream for `finished` must not see one while the job is
            # merely mid-flight, or it transitions the record early.
            out.append({"event": "progress", **counters})
            return out

        if errored is not None:
            status = TERMINAL_NEEDS_INPUT if _is_input_request(errored) else TERMINAL_FAILED
            summary = errored
        elif not saw_terminal:
            # The run is over and the provider never closed its stream. Claiming
            # completed here is precisely the shape atelier tripwires on.
            status = TERMINAL_FAILED
            summary = (
                "stream truncated: the run ended without a terminal provider event, "
                "so this result is not evidence of completion"
            )
        elif not last_text.strip():
            # Produced no assistant text. Not a success to report as one -- it is
            # the shape atelier calls completed_empty.
            status = TERMINAL_EMPTY
            summary = ""
        else:
            status = TERMINAL_COMPLETED
            summary = _clip(last_text)

        out.append(
            {
                "event": "finished",
                "status": status,
                **counters,
                "exitSummary": summary,
                "sawTerminal": saw_terminal,
            }
        )
        return out


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


_ADAPTERS = {"opencode": OpenCodeAdapter()}


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
