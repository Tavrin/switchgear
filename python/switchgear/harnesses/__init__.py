"""Harness adapters: the seam where provider-specific shapes stop.

Everything above this package -- the digest, `logs`, `status`, and anything a
caller consumes -- works on a NORMALIZED vocabulary. Everything below it is one
agent CLI's private format. That is the structural advantage over a wrapper that
passes a raw format through and would need the digest written once per provider.

The normalized vocabulary, from docs/OBSERVABILITY.md:

    {"event": "status",   "v": int, "sessionId": str}
    {"event": "tool",     "v": int, "name": str, "target": str}
    {"event": "text",     "v": int, "content": str}  # truncated at write time
    {"event": "progress", "v": int, "turns": int,
                            "costUSD": float, "tokens": int}
    {"event": "finished", "v": int, "status": str,
                            "final_text_state": str, "turns": int,
                            "costUSD": float, "tokens": int, "exitSummary": str}

Every event carries the `event` discriminator. `v` is stamped on by whoever
writes the stream out -- `job._write_normalized_events` into
`evidence/events.v2.jsonl`, and `logs --format normalized` -- not by an adapter,
which returns the events themselves. Historical `events.v1.jsonl` artifacts are
never rewritten, and recomputed logs use the version recorded by that job, so v1
remains readable. The byte-capped `logs` digest is a projection over the same
events: each line carries `digest_v`, not normalized-stream `v`.

In v2, `finished.status` is completed / needs_input / failed, while the separate
`final_text_state` is present / empty / unknown. A live v2 run always emits
present or empty; unknown is only the honest consumer-side upgrade of a v1
needs_input or failed event, because v1 discarded text presence for those
outcomes.

The exact v2 -> v1 terminal mapping is (completed,present) -> completed;
(completed,empty) -> completed_empty; (needs_input,present) and
(needs_input,empty) -> needs_input; (failed,present) and (failed,empty) ->
failed. The exact consumer-side v1 -> v2 mapping is completed ->
(completed,present), completed_empty -> (completed,empty), needs_input ->
(needs_input,unknown), and failed -> (failed,unknown). Non-terminal events are
unchanged apart from `v`.

`sessionId` is the single most load-bearing field: without a durable session
identifier there is no resume, and a reply to a finished job cannot exist.

`changed_files` is deliberately NOT an event field, and never comes from the
worker. Within this contract, change presence is `change.state` and
`freeze.changed_files` -- computed by the controller from the worktree, never
inferred from worker text or from `finished.status`. That freeze delta is the
rail's own evidence for its own gates; a caller still derives its result
manifest from git itself and validates the landed tree at merge, and never
trusts an agent-reported file list.

## Layout

One module per harness, because a Claude protocol change should produce a
Claude-shaped diff. The four share almost no byte-identical behaviour, so this is
about review locality, not about factoring out commonality that is not there.

    base.py           the contract, the vocabulary constants, validate_adapter
    normalization.py  helpers shared by more than one harness
    {opencode,grok,claude,codex}.py

Imports run one way: base <- normalization <- harnesses <- here.

Adding one is a static edit here plus a captured real fixture -- deliberately not
a plugin loader. A dynamically loaded third-party adapter would undercut exactly
what this package is relied on for: supply-chain trust, version pinning, adapter
completeness and credential posture.
"""

from __future__ import annotations

from .base import (  # noqa: F401  (re-exported: this is the package's surface)
    EFFORT_SUPPORTED,
    EFFORT_UNMEASURED,
    EFFORT_UNSUPPORTED,
    FINAL_TEXT_EMPTY,
    FINAL_TEXT_PRESENT,
    FINAL_TEXT_UNKNOWN,
    REQUIRED_METHODS,
    TERMINAL_COMPLETED,
    TERMINAL_EMPTY,
    TERMINAL_FAILED,
    TERMINAL_NEEDS_INPUT,
    TEXT_LIMIT,
    HarnessAdapter,
    ProviderAdapter,
    validate_adapter,
)
from .claude import ClaudeCodeAdapter
from .codex import CodexAdapter
from .grok import GrokAdapter
from .normalization import parse_lenient  # noqa: F401  (re-exported)
from .opencode import OpenCodeAdapter

_ADAPTERS = {
    "opencode": OpenCodeAdapter(),
    "grok": GrokAdapter(),
    "claude": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
}


for _name, _adapter in _ADAPTERS.items():
    # At import, so an incomplete adapter cannot reach a job.
    validate_adapter(_name, _adapter)


def get_adapter(provider: str | None):
    """Resolve the CLI adapter for a profile's `provider` field.

    Note this is the PROVIDER BINARY (opencode), not the model pool
    (opencode-go, openrouter) that registry.provider_record resolves. Several
    pools are reached through one binary, so conflating them would pick the
    wrong adapter the moment a second pool appears.
    """
    from ..errors import Refuse

    adapter = _ADAPTERS.get((provider or "").strip().lower())
    if adapter is None:
        raise Refuse(f"no adapter for provider {provider!r} (known: {sorted(_ADAPTERS)})")
    return adapter
