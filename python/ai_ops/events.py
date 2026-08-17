from __future__ import annotations

import json
import re
from typing import Any

from .errors import ProviderError
from .schema import validate

MAX_OUTPUT = 2_000_000

# Real OpenCode 1.18.18 emits: step_start, tool_use, text, step_finish -- and an
# `error` event on failure. It does NOT emit a "complete" event; that vocabulary
# was invented by the committed mock, and the rail matched the mock rather than
# the provider. `complete` stays accepted so the mock and its fixtures keep
# working, but it is not what a live run produces.
TERMINAL_TYPES = {"step_finish", "complete"}
ERROR_TYPES = {"error"}


def _events(text: str) -> list[dict[str, Any]]:
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
        except json.JSONDecodeError as exc:
            raise ProviderError(f"malformed provider JSON at offset {idx}") from exc
        idx = end
        if not isinstance(obj, dict) or "type" not in obj:
            raise ProviderError("provider event missing type")
        if not isinstance(obj["type"], str):
            raise ProviderError("provider event type must be a string")
        out.append(obj)
    if text[idx:].strip():
        raise ProviderError("trailing garbage after provider events")
    return out


def assistant_text(events: list[dict[str, Any]]) -> str:
    """Concatenate the model's own output.

    Structured results (handoff, review verdict) come from here on a live run:
    the provider streams the model's text, it does not synthesise objects of its
    own. The agent prompt is what asks the model to emit JSON.
    """
    chunks: list[str] = []
    for ev in events:
        part = ev.get("part")
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            chunks.append(str(part["text"]))
        elif ev.get("type") == "text" and isinstance(ev.get("text"), str):
            chunks.append(ev["text"])
    return "\n".join(chunks)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_object(text: str, key: str) -> dict[str, Any] | None:
    """Find a JSON object carrying `key`, from a fenced block or raw braces."""
    candidates: list[str] = [m.group(1) for m in _FENCE.finditer(text)]
    # Also scan bare brace-balanced spans, last first: models often restate.
    depth, start = 0, None
    spans: list[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start : i + 1])
    candidates.extend(reversed(spans))
    for blob in candidates:
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if isinstance(obj, dict) and key in obj:
            inner = obj[key]
            return inner if isinstance(inner, dict) else obj
        if isinstance(obj, dict) and key == "_self":
            return obj
    return None


def parse_event_stream(raw: bytes, *, require_handoff: bool) -> dict[str, Any]:
    if len(raw) > MAX_OUTPUT:
        raise ProviderError("provider output exceeds bound")
    if not raw.strip():
        raise ProviderError("empty provider output")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProviderError(f"provider output is not valid UTF-8: {exc}") from exc

    events = _events(text)
    for ev in events:
        if ev["type"] in ERROR_TYPES:
            msg = ev.get("message")
            if not msg:
                err = ev.get("error")
                if isinstance(err, dict):
                    msg = (err.get("data") or {}).get("message") or err.get("name")
            raise ProviderError(str(msg or "provider error event"))

    terminals = [e for e in events if e["type"] in TERMINAL_TYPES]
    if not terminals:
        raise ProviderError("no terminal provider event")
    # A live run emits one step_finish per step, so the LAST one closes the run.
    # The mock emits exactly one `complete`; duplicates there are still a fault.
    completes = [e for e in terminals if e["type"] == "complete"]
    if len(completes) > 1:
        raise ProviderError("duplicate terminal provider events")
    term = dict(completes[0] if completes else terminals[-1])
    term["_text"] = assistant_text(events)

    if require_handoff:
        handoff = term.get("handoff")
        if not isinstance(handoff, dict):
            handoff = extract_object(term["_text"], "handoff")
        if not isinstance(handoff, dict):
            raise ProviderError("write job missing schema-valid handoff object")
        validate(handoff, "handoff.schema.json")
        if handoff.get("status") not in {"awaiting_review", "complete"}:
            raise ProviderError("wrong handoff status")
        term["_handoff"] = handoff
    return term


def extract_review_verdict(raw: bytes) -> tuple[str, list[dict[str, Any]], list[str]]:
    term = parse_event_stream(raw, require_handoff=False)
    payload = term.get("review")
    if not isinstance(payload, dict):
        payload = extract_object(term.get("_text") or "", "review")
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
