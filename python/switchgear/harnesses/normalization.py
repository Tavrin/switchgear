"""Shared translation into the normalized vocabulary.

Helpers used by more than one harness. Anything here is provider-NEUTRAL by
definition: the moment a helper needs to know which CLI produced the stream it
belongs in that harness's own module.
"""

from __future__ import annotations

import json
from typing import Any

from .base import (
    TERMINAL_COMPLETED,
    TERMINAL_EMPTY,
    TERMINAL_FAILED,
    TERMINAL_NEEDS_INPUT,
    TEXT_LIMIT,
)
from ..errors import ProviderError


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


# Effort ("reasoning effort") capability states.
#
# THREE states, not a boolean, for the same reason session_store_paths() is a
# measured list rather than a flag: "this provider does not support effort" and
# "nobody has measured which values it accepts" are different facts, and
# collapsing them into one produces exactly the guessing this rail forbids.
#
# Effort values are a property of the MODEL, not of the provider, so an adapter
# declares only the MECHANISM -- whether this CLI has an effort control and how
# the value is spelled on its command line. The accepted VALUES live per model in
# switchgear/data/models/registry.json, which is where controller-owned measured facts belong.
#
# That split was forced by measurement rather than chosen for tidiness. The
# OpenAI API's generic enumeration lists `none, minimal, low, medium, high,
# xhigh, max`, but running `minimal` against gpt-5.6-sol was refused with
# "Unsupported value: 'minimal' is not supported with the 'gpt-5.6-sol' model".
# One provider, two models, two different sets -- so any provider-level list is
# wrong for some model in the pool, and would be wrong silently.
#
# How each CLI fails a bad value, measured 2026-08-18 on the pinned builds:
#
#   grok     rejects CLIENT-SIDE before any API call, naming its own set:
#            "unknown effort level 'x'; use one of: high, medium, low". Free.
#   codex    forwards it; the API rejects with HTTP 400 and enumerates the set
#            FOR THAT MODEL. Costs one failed turn.
#   claude   --effort is enumerated in its own --help.
#   opencode ACCEPTED `--variant not-a-real-value` and ran the job to completion,
#            returning a real answer at full price ($0.0022). It neither
#            validates nor reports -- the value is simply dropped.
#
# That last case is the whole argument for refusing an unmeasured value instead
# of passing it through and hoping: a provider that silently ignores an effort it
# does not understand hands back a job that ran at the model's default while the
# record claims otherwise. A lie in the evidence, bought at full price.

def _extract_review(text: str):
    """Pull the reviewer's verdict object out of its final text.

    The mirror of _extract_handoff, and it exists for the same reason: only
    OpenCode emits structured objects of its own, so for every other provider the
    verdict has to be recovered from the model's own words.
    """
    from ..events import extract_object

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
    from ..events import extract_object
    from ..schema import validate

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
    "claims done, evidence truncated" is the case the orchestrator tripwires on); then a
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

    Kept minimal on purpose: the orchestrator renders tool events as plain text, so a
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


