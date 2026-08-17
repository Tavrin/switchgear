from __future__ import annotations

import json
from typing import Any

from .errors import ProviderError
from .schema import validate

MAX_OUTPUT = 2_000_000
TERMINAL = {"complete", "error"}


def parse_event_stream(raw: bytes, *, require_handoff: bool) -> dict[str, Any]:
    if len(raw) > MAX_OUTPUT:
        raise ProviderError("provider output exceeds bound")
    if not raw.strip():
        raise ProviderError("empty provider output")
    text = raw.decode("utf-8", errors="strict")
    # Reject concatenated non-JSONL blobs by requiring newline-delimited objects
    # plus no trailing leftover after the last parse.
    terminals: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
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
            # `x in TERMINAL` raises TypeError on an unhashable value, which
            # would escape as an uncaught controller crash with no result record.
            raise ProviderError("provider event type must be a string")
        if obj["type"] in TERMINAL:
            terminals.append(obj)
    # leftover non-space is truncated/garbage tail
    tail = text[idx:]
    if tail.strip():
        raise ProviderError("trailing garbage after provider events")
    if len(terminals) == 0:
        raise ProviderError("no terminal provider event")
    if len(terminals) != 1:
        raise ProviderError("duplicate terminal provider events")
    term = terminals[0]
    if term["type"] == "error":
        raise ProviderError(term.get("message") or "provider error event")
    if require_handoff:
        handoff = term.get("handoff")
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
